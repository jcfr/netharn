"""
python -c "import ubelt._internal as a; a.autogen_init('netharn.util')"
python -m netharn
"""
# flake8: noqa
from __future__ import absolute_import, division, print_function, unicode_literals

__DYNAMIC__ = True
if __DYNAMIC__:
    from ubelt._internal import dynamic_import
    exec(dynamic_import(__name__))
else:
    # <AUTOGEN_INIT>
    from netharn.util import imutil
    from netharn.util import mplutil
    from netharn.util import nms
    from netharn.util import profiler
    from netharn.util import torch_utils
    from netharn.util import util_averages
    from netharn.util import util_boxes
    from netharn.util import util_demodata
    from netharn.util import util_fname
    from netharn.util import util_idstr
    from netharn.util import util_json
    from netharn.util import util_random
    from netharn.util import util_resources
    from netharn.util.imutil import (CV2_INTERPOLATION_TYPES, adjust_gamma,
                                     atleast_3channels, convert_colorspace,
                                     ensure_alpha_channel, ensure_float01,
                                     ensure_grayscale, get_num_channels,
                                     grab_test_imgpath, image_slices, imread,
                                     imscale, imwrite, load_image_paths,
                                     make_channels_comparable,
                                     overlay_alpha_images, overlay_colorized,
                                     putMultiLineText, run_length_encoding,
                                     wide_strides_1d,)
    from netharn.util.mplutil import (Color, PlotNums, adjust_subplots,
                                      axes_extent, colorbar,
                                      copy_figure_to_clipboard,
                                      deterministic_shuffle, dict_intersection,
                                      distinct_colors, distinct_markers,
                                      draw_border, draw_boxes, draw_line_segments,
                                      ensure_fnum, extract_axes_extents, figure,
                                      imshow, legend, multi_plot, next_fnum,
                                      pandas_plot_matrix, qtensure,
                                      render_figure_to_image, reverse_colormap,
                                      save_parts, savefig2, scores_to_cmap,
                                      scores_to_color, set_figtitle,
                                      show_if_requested,)
    from netharn.util.nms import (non_max_supression,)
    from netharn.util.profiler import (IS_PROFILING, IS_PROFILING, KernprofParser,
                                       dump_global_profile_report, dynamic_profile,
                                       find_parent_class, find_pattern_above_row,
                                       find_pyclass_above_row, profile, profile,
                                       profile_onthefly,)
    from netharn.util.torch_utils import (grad_context, number_of_parameters,)
    from netharn.util.util_averages import (CumMovingAve, ExpMovingAve,
                                            InternalRunningStats, MovingAve,
                                            RunningStats, WindowedMovingAve,
                                            absdev,)
    from netharn.util.util_boxes import (Boxes, clip_boxes, random_boxes,
                                         scale_boxes,)
    from netharn.util.util_demodata import (grab_test_image,)
    from netharn.util.util_fname import (align_paths, check_aligned, dumpsafe,
                                         shortest_unique_prefixes,
                                         shortest_unique_suffixes,)
    from netharn.util.util_idstr import (compact_idstr, make_idstr,
                                         make_short_idstr,)
    from netharn.util.util_json import (JSONEncoder, NumpyAwareJSONEncoder,
                                        NumpyEncoder, read_json, walk_json,
                                        write_json,)
    from netharn.util.util_random import (ensure_rng,)
    from netharn.util.util_resources import (ensure_ulimit,)