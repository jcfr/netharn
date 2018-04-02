# -*- coding: utf-8 -*-
"""
CommandLine:
    python ~/code/netharn/netharn/fit_harn.py __doc__

Example:
    >>> import netharn as nh
    >>> datasets = {
    >>>     'train': nh.data.ToyData2d(size=3, border=1, n=256, rng=0),
    >>>     'vali': nh.data.ToyData2d(size=3, border=1, n=128, rng=1),
    >>> }
    >>> hyper = {
    >>>     # --- Data First
    >>>     'datasets'    : datasets,
    >>>     'nice'        : 'demo',
    >>>     'workdir'     : ub.ensure_app_cache_dir('netharn/demo'),
    >>>     'loaders'     : {'batch_size': 64},
    >>>     'xpu'         : nh.XPU.cast('auto'),
    >>>     # --- Algorithm Second
    >>>     'model'       : (nh.models.ToyNet2d, {}),
    >>>     'optimizer'   : (nh.optimizers.SGD, {
    >>>         'lr': 0.001
    >>>     }),
    >>>     'criterion'   : (nh.criterions.CrossEntropyLoss, {}),
    >>>     #'criterion'   : (nh.criterions.FocalLoss, {}),
    >>>     'initializer' : (nh.initializers.KaimingNormal, {
    >>>         'param': 0,
    >>>     }),
    >>>     'scheduler'   : (nh.schedulers.ListedLR, {
    >>>         'points': {0: .001, 2: .01, 5: .015, 6: .005, 9: .001},
    >>>         'interpolate': True,
    >>>     }),
    >>>     'monitor'     : (nh.Monitor, {
    >>>         'max_epoch': 10
    >>>     }),
    >>> }
    >>> harn = FitHarn(hyper)
    >>> harn.initialize(reset='delete')
    >>> harn.run()
"""
import glob
import itertools as it
import logging  # NOQA
import numpy as np  # NOQA
import os
import parse
import shutil
import time
import torch
import ubelt as ub
import sys
from os.path import join

from netharn import device  # NOQA
from netharn import folders
from netharn import hyperparams

from netharn import util
from netharn.util import profiler

__all__ = ['FitHarn']


MIXINS = []


def register_mixin(cls):
    MIXINS.append(cls)
    return cls


@register_mixin
class ConstructorMixin:
    def __init__(harn, hyper):
        if isinstance(hyper, dict):
            hyper = hyperparams.HyperParams(**hyper)
        harn.hyper = hyper

        harn._main_prog = None

        harn.datasets = None
        harn.loaders = None

        harn.model = None
        harn.optimizer = None
        harn.scheduler = None
        harn.monitor = None
        harn.criterion = None

        harn.paths = None

        harn._initialized = False
        harn.flog = None

        harn.dry = None

        harn.intervals = {
            'display_train': 1,
            'display_vali': 1,
            'display_test': 1,

            'vali': 1,
            'test': 1,

            'snapshot': 1,
            'cleanup': 1,
        }
        harn.config = {
            'show_prog': True,
            'use_tqdm': True,
            'log_iter_values': True,
        }
        harn.epoch = 0
        harn.bxs = {
            'train': 0,
            'vali': 0,
            'test': 0,
        }
        harn.current_tag = None

    def check_interval(harn, tag, idx):
        """
        check if its time to do something that happens every few iterations
        """
        return (idx + 1) % harn.intervals[tag] == 0


@register_mixin
class ExtraMixins:
    def _demo_batch(harn, index=0, tag='train'):
        """
        returns a single batch for testing / demo purposes.
        """
        loader = harn.loaders[tag]
        harn.current_tag = tag
        for bx, batch in enumerate(iter(loader)):
            if bx >= index:
                break
        return harn.prepare_batch(batch)


@register_mixin
class InitializeMixin:
    def setup_paths(harn):
        paths = folders.Folders(hyper=harn.hyper)
        train_info = paths.setup_dpath()
        harn.paths = paths
        harn.train_info = train_info
        harn.nice_dpath = train_info['nice_dpath']
        harn.train_dpath = train_info['train_dpath']
        harn.link_dpath = train_info['link_dpath']

        return harn.train_dpath

    def setup_flogger(harn):
        flog_fname = 'fitlog_{}.log'.format(ub.timestamp())
        flog_fpath = os.path.join(harn.train_dpath, flog_fname)
        flog = logging.getLogger(harn.__class__.__name__)
        formatter = logging.Formatter('%(asctime)s : %(message)s')
        handler = logging.FileHandler(flog_fpath, mode='w')
        handler.setFormatter(formatter)
        flog.propagate = False
        flog.setLevel(logging.DEBUG)
        flog.addHandler(handler)
        harn.flog = flog
        harn.debug('initialized file logger')

    def initialize(harn, reset=False):
        """
        Uses the hyper parameters to initialize the necessary resources and
        restart from previously
        """

        if harn.paths is None:
            harn.setup_paths()

        if reset == 'delete':
            ub.delete(harn.train_dpath)
            ub.ensuredir(harn.train_dpath)

        use_file_logger = True
        if use_file_logger and harn.flog is None:
            harn.setup_flogger()

        harn.debug('make XPU')
        harn.xpu = harn.hyper.make_xpu()
        harn.debug('harn.xpu = {!r}'.format(harn.xpu))
        harn.xpu.set_as_default()

        import tensorboard_logger
        if tensorboard_logger:
            train_base = os.path.dirname(harn.nice_dpath or harn.train_dpath)
            harn.log('dont forget to start: tensorboard --logdir ' + train_base)
            harn.log('Initializing tensorboard')
            harn.tlogger = tensorboard_logger.Logger(harn.train_dpath,
                                                     flush_secs=2)

        prev_states = harn.prev_snapshots()

        model_name = harn.hyper.model_cls.__name__

        if harn.hyper.criterion_cls:
            harn.log('Criterion: {}'.format(harn.hyper.criterion_cls.__name__))
        else:
            harn.log('Criterion: Custom')

        harn.log('Optimizer: {}'.format(harn.hyper.optimizer_cls.__name__))

        if harn.hyper.scheduler_cls:
            harn.log('Scheduler: {}'.format(harn.hyper.scheduler_cls.__name__))
        else:
            harn.log('No Scheduler')

        harn.debug('Making loaders')
        harn.datasets = harn.hyper.datasets
        harn.loaders = harn.hyper.make_loaders()

        harn.debug('Making model')
        harn.model = harn.hyper.make_model()
        harn.initializer = harn.hyper.make_initializer()

        harn.log('Mounting {} model on {}'.format(model_name, harn.xpu))
        harn.model = harn.xpu.mount(harn.model)

        n_params = util.number_of_parameters(harn.model)
        harn.log('Model has {!r} parameters'.format(n_params))

        harn.criterion = harn.hyper.make_criterion()
        if harn.criterion:
            harn.log('Move {} model to {}'.format(harn.criterion, harn.xpu))
            harn.criterion = harn.xpu.move(harn.criterion)

        harn.log('Make optimizer')
        harn.optimizer = harn.hyper.make_optimizer(harn.model.parameters())

        harn.log('Make scheduler')
        harn.scheduler = harn.hyper.make_scheduler(harn.optimizer)

        harn.log('Make monitor')
        harn.monitor = harn.hyper.make_monitor()

        needs_init = True
        harn.log('There are {} existing snapshots'.format(len(prev_states)))
        if prev_states and not reset:
            harn.log('Loading previous states')
            # Ignore corrupted snapshots
            for load_path in reversed(prev_states):
                try:
                    harn.load_snapshot(load_path)
                except RuntimeError:
                    harn.log('Failed to load {}. Skiping.'.format(load_path))
                else:
                    needs_init = False
                    break
            for i, group in enumerate(harn.optimizer.param_groups):
                if 'initial_lr' not in group:
                    raise KeyError("param 'initial_lr' is not specified "
                                   "in param_groups[{}] when resuming an optimizer".format(i))

        if needs_init:
            harn.log('Initializing new model')
            if harn.initializer.__class__.__name__ == 'LSUV':
                # harn.model = harn.xpu.mount(harn.model)
                # set([p.is_cuda for p in harn.model.parameters()])

                #hack LSUV needs a batch of data to run
                with util.grad_context(False):
                    # import utool
                    # utool.embed()
                    loader = harn.loaders['train']
                    input, labels = next(iter(loader))
                    data = harn.xpu.variable(input)
                    harn.initializer(harn.model, data)
            else:
                harn.initializer(harn.model)
            if not harn.dry:
                for group in harn.optimizer.param_groups:
                    group.setdefault('initial_lr', group['lr'])

        harn.log('Snapshots will save to harn.snapshot_dpath = {!r}'.format(harn.snapshot_dpath))
        harn._initialized = True


@register_mixin
class ProgMixin:
    def _make_prog(harn, **kw):
        if harn.config['use_tqdm']:
            import tqdm
            Prog = tqdm.tqdm
        else:
            import functools
            Prog = functools.partial(ub.ProgIter, verbose=1)
        return Prog(**kw)

    def _batch_msg(harn, metric_dict, batch_size):
        bs = 'x{}'.format(batch_size)
        metric_parts = ['{}:{:.3f}'.format(k, v) for k, v in metric_dict.items()]
        msg = ' │ ' .join([bs] + metric_parts) + ' │'
        return msg

    def _close_prog(harn):
        if harn.main_prog is not None:
            harn.main_prog.close()
            harn.main_prog = None
            sys.stdout.write('\n\n\n\n')  # fixes progress bar formatting

    def _update_prog_desc(harn):
        lrs = harn._current_lrs()
        lr_str = ','.join(['{:.2g}'.format(lr) for lr in lrs])
        desc = 'epoch lr:{} │ {}'.format(lr_str, harn.monitor.message())
        harn.debug(desc)
        harn.main_prog.set_description(desc, refresh=False)
        if isinstance(harn.main_prog, ub.ProgIter):
            if not harn.main_prog.started:
                # harn.main_prog.ensure_newline()
                harn.main_prog.clearline = False
                harn.main_prog.freq = 1
                harn.main_prog.adjust = False
                harn.main_prog.begin()
        else:
            harn.main_prog.set_postfix(
                {'wall': time.strftime('%h:%m') + ' ' + time.tzname[0]},
                refresh=False)
            # update tqdm, but let progiter refresh itself
            harn.main_prog.refresh()


@register_mixin
class LogMixin:
    def log(harn, msg):
        harn.debug(msg)
        print(msg)

    def debug(harn, msg):
        if harn.flog:
            from xdoctest.utils import strip_ansi
            msg = strip_ansi(msg)
            harn.flog.debug(msg)

    def error(harn, msg):
        if harn.flog:
            from xdoctest.utils import strip_ansi
            msg = strip_ansi(msg)
            harn.flog.error(msg)

    def log_value(harn, key, value, n_iter):
        if harn.tlogger:
            harn.tlogger.log_value(key, value, n_iter)
        harn.debug('log_value({}, {}, {}'.format(key, value, n_iter))

    def log_histogram(harn, key, value, n_iter):
        if harn.tlogger:
            harn.tlogger.log_histogram(key, value, n_iter)

    def log_images(harn, key, value, n_iter):
        if harn.tlogger:
            harn.tlogger.log_images(key, value, n_iter)


@register_mixin
class SnapshotMixin:

    @property
    def snapshot_dpath(harn):
        return join(harn.train_dpath, 'torch_snapshots')

    def cleanup_snapshots(harn):
        """
        remove old snapshots
        """
        snapshots = harn.prev_snapshots()
        epochs = [parse.parse('{}_epoch_{num:d}.pt', path).named['num']
                  for path in snapshots]

        def _epochs_to_remove(epochs):
            """
            doctest:
                >>> harn = FitHarn()
                >>> rng = np.random.RandomState(0)
                >>> for epoch in range(200):
                >>>     harn.monitor.update(epoch, {'loss': rng.rand(),
                >>>                                 'miou': rng.rand()})
                >>> epochs = list(range(0, 200, 4))
            """
            num_keep_recent = 10
            num_keep_best = 10

            keep = set()

            recent = epochs[-num_keep_recent:]
            keep.update(recent)

            if harn.monitor:
                best_epochs = harn.monitor.best_epochs()
                best = ub.oset(best_epochs).intersection(epochs)
                keep.update(best[-num_keep_best:])

            to_remove = set(epochs) - keep
            return to_remove

        epoch_to_fpath = dict(zip(epochs, snapshots))
        to_remove = _epochs_to_remove(epochs)
        for fpath in ub.take(epoch_to_fpath, to_remove):
            ub.delete(fpath)

    def prev_snapshots(harn):
        ub.ensuredir(harn.snapshot_dpath)
        prev_states = sorted(glob.glob(join(harn.snapshot_dpath, '_epoch_*.pt')))
        return prev_states

    def load_snapshot(harn, load_path):
        """
        Sets the harness to its state just after an epoch finished
        """
        harn.log('Loading previous state: {}'.format(load_path))
        snapshot = harn.xpu.load(load_path)
        # the snapshot holds the previous epoch, so add one to move to current
        harn.epoch = snapshot['epoch'] + 1
        harn.model.load_state_dict(snapshot['model_state_dict'])
        harn.debug('loaded model_state_dict')

        if 'monitor_state_dict' in snapshot:
            # Dont override patience, use whatever the current val is
            patience = harn.monitor.patience
            harn.monitor.load_state_dict(snapshot['monitor_state_dict'])
            harn.monitor.patience = patience
            harn.debug('loaded monitor_state_dict')

        if 'optimizer_state_dict' in snapshot:
            harn.optimizer.load_state_dict(snapshot['optimizer_state_dict'])
            harn.debug('loaded optimizer_state_dict')
        harn.log('Resuming training...')

    def save_snapshot(harn):
        # save snapshot
        ub.ensuredir(harn.snapshot_dpath)
        save_path = join(harn.snapshot_dpath, '_epoch_{:08d}.pt'.format(harn.epoch))
        if harn.dry:
            harn.debug('Would save snapshot to {}'.format(save_path))
        else:
            train = harn.datasets['train']
            if hasattr(train, 'dataset_metadata'):
                dataset_metadata = train.dataset_metadata()
            else:
                dataset_metadata = None

            # TODO: should we split the optimizer state into a different file?
            snapshot = {
                'model_class_name': harn.model.__class__.__name__,
                'dataset_metadata': dataset_metadata,
                'epoch': harn.epoch,
                'model_state_dict': harn.model.state_dict(),
                'optimizer_state_dict': harn.optimizer.state_dict(),
                'monitor_state_dict': harn.monitor.state_dict(),
            }
            torch.save(snapshot, save_path)
            harn.debug('Snapshot saved to {}'.format(save_path))
            return save_path


@register_mixin
class ScheduleMixin:

    def _current_lrs(harn):
        if harn.scheduler is None:
            if harn.optimizer is None:
                assert harn.dry
                lrs = [.01]
            else:
                lrs = set(map(lambda group: group['lr'], harn.optimizer.param_groups))
        elif hasattr(harn.scheduler, '_current_lrs'):
            lrs = set(harn.scheduler._current_lrs())
        elif hasattr(harn.scheduler, 'get_lr'):
            lrs = set(harn.scheduler.get_lr())
        else:
            # workaround for reducelronplateau
            lrs = {group['lr'] for group in harn.scheduler.optimizer.param_groups}
        return lrs

    def backtrack_weights(harn, epoch):
        """
        Reset the weights to a previous good state
        """
        load_path = join(harn.snapshot_dpath, '_epoch_{:08d}.pt'.format(epoch))
        snapshot = harn.xpu.load(load_path)

        print('\n\n\n\n')
        harn.log('Backtracking to weights from previous state: {}'.format(load_path))
        # only load the model state, the optimizer and other state items stay
        # as is.
        harn.model.load_state_dict(snapshot['model_state_dict'])

    def _check_termination(harn):
        # if harn.epoch >= harn.config['max_epoch']:
        if harn.epoch >= harn.monitor.max_epoch:
            harn._close_prog()
            harn.log('Maximum harn.epoch reached, terminating ...')
            return True
        if harn.monitor.is_done():
            harn._close_prog()
            harn.log('Validation set is not improving, terminating ...')
            return True
        return False

    def _step_scheduler(harn, improved):
        """
        helper function to change the learning rate that handles the way that
        different schedulers might be used.
        """
        if harn.scheduler is None:
            pass
        elif harn.scheduler.__class__.__name__ == 'reducelronplateau':
            assert improved is not None, 'must validate for ReduceLROnPlateau schedule'
            # assert vali_metrics is not None, (
            #     'must validate for ReduceLROnPlateau schedule')

            # old_lrs = set(harn._current_lrs())
            # feed reduce on plateau dummy data from the monitor
            # harn.scheduler.step(vali_metrics['loss'])

            # harn.scheduler.step(vali_metrics['loss'], epoch=harn.epoch)
            def hack_lr_step(self, improved, epoch=None):
                if epoch is None:
                    epoch = self.last_epoch = self.last_epoch + 1
                self.last_epoch = epoch

                if improved:
                    self.num_bad_epochs = 0
                else:
                    self.num_bad_epochs += 1

                if self.in_cooldown:
                    self.cooldown_counter -= 1
                    self.num_bad_epochs = 0  # ignore any bad epochs in cooldown

                if self.num_bad_epochs > self.patience:
                    self._reduce_lr(epoch)
                    self.cooldown_counter = self.cooldown
                    self.num_bad_epochs = 0

                    # todo: make a pytorch pr where there is a callback on
                    # lr_reduction.
                    # the scheduler has stepped, we should now backtrack the
                    # weights to the previous best state
                    backtrack = False
                    if backtrack:
                        harn.backtrack_weights(harn.monitor.best_epoch)

            # # hack to determine if the rlrop scheduler stepped
            hack_lr_step(harn.scheduler, improved)

            # new_lrs = set(harn._current_lrs())
            # if old_lrs != new_lrs:
            #     # the scheduler has stepped, we should now backtrack the
            #     # weights to the previous best state
            #     harn.backtrack_weights(harn.monitor.best_epoch)
        else:
            harn.scheduler.step()


@register_mixin
class CoreMixin:
    """
    The core main execution loop
    """
    @profiler.profile
    def run(harn):
        """
        main training loop
        """
        if not harn._initialized:
            harn.initialize()

        harn.log('begin training')

        if harn._check_termination():
            return

        harn.main_prog = harn._make_prog(desc='epoch',
                                         # total=harn.config['max_epoch'],
                                         total=harn.monitor.max_epoch,
                                         disable=not harn.config['show_prog'],
                                         leave=True, dynamic_ncols=True,
                                         position=1, initial=harn.epoch)
        harn._update_prog_desc()

        train_loader = harn.loaders['train']
        vali_loader  = harn.loaders.get('vali', None)
        test_loader  = harn.loaders.get('test', None)

        if not vali_loader:
            if not harn.scheduler:
                if harn.monitor:
                    raise ValueError('need a validataion dataset to use early monitor')
                if harn.scheduler.__class__.__name__ == 'reducelronplateau':
                    raise ValueError('need a validataion dataset to use reducelronplateau')

        # keep track of moving metric averages across epochs
        harn._run_metrics = {
            tag: util.WindowedMovingAve(window=len(loader))
            for tag, loader in harn.loaders.items()
        }

        # if harn.scheduler:
        #     # prestep scheduler?
        #     if getattr(harn.scheduler, 'last_epoch', 0) == -1:
        #         harn.scheduler.step()

        try:
            for harn.epoch in it.count(harn.epoch):
                harn.debug('=== start epoch {} ==='.format(harn.epoch))

                harn.log_value('epoch lr', np.mean(list(harn._current_lrs())),
                               harn.epoch)

                # run training epoch
                harn._run_epoch(train_loader, tag='train', learn=True)

                # run validation epoch
                vali_metrics = None
                improved = False
                if vali_loader:
                    if harn.check_interval('vali', harn.epoch):
                        vali_metrics = harn._run_epoch(
                            vali_loader, tag='vali', learn=False)
                        improved = harn.monitor.update(harn.epoch,
                                                       vali_metrics)

                    harn._update_prog_desc()

                # run test epoch
                if test_loader:
                    if harn.check_interval('test', harn.epoch):
                        harn._run_epoch(test_loader, tag='test', learn=False)

                if improved:
                    save_path = harn.save_snapshot()
                    if save_path:
                        harn.debug('new best_snapshot {}'.format(save_path))
                        # copy the best snapshot the the main directory
                        best_path = join(harn.train_dpath, 'best_snapshot.pt')
                        shutil.copy2(save_path, best_path)
                else:
                    # todo: allow monitor to clean up old snapshots
                    if harn.check_interval('snapshot', harn.epoch):
                        save_path = harn.save_snapshot()

                if harn.check_interval('cleanup', harn.epoch):
                    harn.cleanup_snapshots()

                harn.main_prog.update(1)

                # check for termination
                if harn._check_termination():
                    raise StopIteration()

                # change learning rate (modified optimizer inplace)
                harn._step_scheduler(improved)

                harn._update_prog_desc()
        except StopIteration:
            pass
        except Exception as ex:
            harn.log('an {} error occurred in the train loop'.format(type(ex)))
            harn._close_prog()
            raise

        harn.log('\n\n\n')
        harn.log('training completed')
        harn.log('current lrs: {}'.format(harn._current_lrs()))
        # harn.log('best epochs / loss: {}'.format(
        #     ub.repr2(list(harn.monitor.memory), nl=1)))
        harn.log('exiting harness.')

    @profiler.profile
    def _run_epoch(harn, loader, tag, learn=False):
        """
        evaluate the model on test / train / or validation data
        """
        harn.debug('_run_epoch {}, tag={}, learn={}'.format(harn.epoch, tag, learn))
        harn.debug(' * len(loader) = {}'.format(len(loader)))
        harn.debug(' * loader.batch_size = {}'.format(loader.batch_size))

        harn.current_tag = tag

        # use exponentially weighted or windowed moving averages across epochs
        iter_moving_metrics = harn._run_metrics[tag]
        # use simple moving average within an epoch
        epoch_moving_metrics = util.CumMovingAve()

        # train batch
        if not harn.dry:
            # flag if model is training (influences batch-norm / dropout)
            if harn.model.training != learn or learn:
                harn.model.train(learn)

        msg = harn._batch_msg({'loss': -1}, loader.batch_sampler.batch_size)
        desc = tag + ' ' + msg
        position = (list(harn.loaders.keys()).index(tag) +
                    harn.main_prog.pos + 1)
        prog = harn._make_prog(desc=desc, total=len(loader), disable=not
                               harn.config['show_prog'], position=position,
                               leave=True, dynamic_ncols=True)
        needs_postfix = not isinstance(prog, ub.ProgIter)
        if needs_postfix:
            prog.set_postfix({'wall': time.strftime('%h:%m') + ' ' + time.tzname[0]})

        with util.grad_context(learn):
            batch_iter = iter(loader)
            for bx in range(len(loader)):
                batch = next(batch_iter)

                harn.bxs[tag] = bx
                batch = harn.prepare_batch(batch)

                # core learning / backprop
                outputs, loss = harn._run_batch(batch, learn=learn)

                # measure train accuracy and other informative metrics
                cur_metrics = harn._on_batch(batch, outputs, loss)

                # accumulate measures
                epoch_moving_metrics.update(cur_metrics)
                iter_moving_metrics.update(cur_metrics)

                # display_train training info
                if harn.check_interval('display_' + tag, bx):
                    ave_metrics = iter_moving_metrics.average()

                    msg = harn._batch_msg({'loss': ave_metrics['loss']},
                                          loader.batch_sampler.batch_size)
                    prog.set_description(tag + ' ' + msg)

                    if harn.config['log_iter_values']:
                        iter_idx = (harn.epoch * len(loader) + bx)
                        for key, value in ave_metrics.items():
                            harn.log_value(tag + ' iter ' + key, value, iter_idx)

                    prog.update(harn.intervals['display_' + tag])
                    if needs_postfix:
                        prog.set_postfix({'wall': time.strftime('%h:%m') + ' ' + time.tzname[0]})

        prog.close()

        # record a True average for the entire batch
        epoch_metrics = epoch_moving_metrics.average()

        # call hooks after every epoch
        custom_metrics = harn.on_epoch()
        if custom_metrics:
            isect = set(custom_metrics).intersection(set(epoch_metrics))
            if isect:
                raise Exception('Conflicting epoch metrics: {}'.format(isect))
            epoch_metrics.update(custom_metrics)

        for key, value in epoch_metrics.items():
            harn.log_value(tag + ' epoch ' + key, value, harn.epoch)

        return epoch_metrics

    @profiler.profile
    def _run_batch(harn, batch, learn=False):
        """
        batch with weight updates

        https://github.com/meetshah1995/pytorch-semseg/blob/master/train.py
        """
        try:
            outputs, loss = harn.run_batch(batch)
        except Exception:
            harn.error('may need to make a custom batch runner with set_batch_runner')
            raise

        # backprop and learn
        if learn:
            harn.optimizer.zero_grad()
            loss.backward()
            harn.optimizer.step()

        return outputs, loss

    def _on_batch(harn, batch, outputs, loss):
        """
        Overload Encouraged
        """
        loss_sum = float(loss.data.sum().cpu())
        inf = float("inf")

        # FIXME: this check needs improvement
        if loss_sum == inf or loss_sum == -inf:
            harn.log("WARNING: received an inf loss, setting loss value to 0")
            loss_value = 0
        else:
            loss_value = loss_sum

        if harn.current_tag == 'train' and float(loss) > 1000:
            # if the loss is getting very larg, check that the weights are
            # still ok
            state = harn.model.module.state_dict()
            weights = sum([v.sum() for v in state.values()])
            if (not np.isfinite(weights) or np.isnan(weights)):
                raise Exception('NON-FINITE WEIGHTS weights = {!r}'.format(weights))

        metrics_dict = {
            'loss': loss_value,
        }
        custom_metrics = harn.on_batch(batch, outputs, loss)
        if custom_metrics:
            isect = set(custom_metrics).intersection(set(metrics_dict))
            if isect:
                raise Exception('Conflicting batch metrics: {}'.format(isect))
            metrics_dict.update(custom_metrics)

        return metrics_dict


@register_mixin
class CoreCallback:
    """
    We encourage you to overwrite these methods
    """

    def _tovar(harn, data):
        # handle cases when labels are unstructured
        if isinstance(data, list):
            # handle one level of nesting
            return [harn.xpu.variable(d) for d in data]
        else:
            return harn.xpu.variable(data)

    def prepare_batch(harn, batch):
        """
        ensure batch is in a standardized structure

        Overload Encouraged, but not always necessary
        """
        batch_inputs, batch_labels = batch

        # the dataset should return a inputs/target 2-tuple of lists.
        # in most cases each list will be length 1, unless there are
        # multiple input branches or multiple output branches.
        if not isinstance(batch_inputs, (list, tuple)):
            batch_inputs = [batch_inputs]
        if not isinstance(batch_labels, (list, tuple)):
            batch_labels = [batch_labels]

        inputs = [harn.xpu.variable(d) for d in batch_inputs]
        labels = [harn._tovar(d) for d in batch_labels]

        prepared_batch = (inputs, labels)
        return prepared_batch

    def run_batch(harn, batch):
        """
        Basic connection inputs -> model -> outputs -> criterion -> loss

        Overload Encouraged, but not always necessary
        """
        # Simple forward prop and loss computation
        inputs, labels = batch
        outputs = harn.model(*inputs)
        loss = harn.criterion(outputs, *labels)
        return outputs, loss

    def on_batch(harn, batch, outputs, loss):
        """
        custom callback typically used to compute batch evaluation measures
        or accumulate data.

        Overload Encouraged
        """
        pass

    def on_epoch(harn):
        """
        custom callback typically used to compute epoch evaluation measures.

        If a dict is returned they are added to epoch measures

        Overload Encouraged
        """
        pass


# Define the exposed class as a union of mixin classes
class FitHarn(*MIXINS):
    """
    Args:
        hyper (netharn.HyperParams): Parameters that determine the system.
            This serializable class encodes enough information to
            deterministically reproduce an experiment.

            Because it is serializable it also has an easy to use dict
            representation.
    """

if __name__ == '__main__':
    """
    CommandLine:
        python -m netharn.fit_harn all
    """
    import xdoctest
    xdoctest.doctest_module(__file__)