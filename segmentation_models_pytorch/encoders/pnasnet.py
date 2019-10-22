from collections import OrderedDict

import torch
import torch.nn as nn
import torch.utils.model_zoo as model_zoo

from ..common.weights import select_rgb_weights


class MaxPool(nn.Module):

    def __init__(self, kernel_size, stride=1, padding=1, zero_pad=False):
        super(MaxPool, self).__init__()
        self.zero_pad = nn.ZeroPad2d((1, 0, 1, 0)) if zero_pad else None
        self.pool = nn.MaxPool2d(kernel_size, stride=stride, padding=padding)

    def forward(self, x):
        if self.zero_pad:
            x = self.zero_pad(x)
        x = self.pool(x)
        if self.zero_pad:
            x = x[:, :, 1:, 1:]
        return x


class SeparableConv2d(nn.Module):

    def __init__(self, in_channels, out_channels, dw_kernel_size, dw_stride,
                 dw_padding):
        super(SeparableConv2d, self).__init__()
        self.depthwise_conv2d = nn.Conv2d(in_channels, in_channels,
                                          kernel_size=dw_kernel_size,
                                          stride=dw_stride, padding=dw_padding,
                                          groups=in_channels, bias=False)
        self.pointwise_conv2d = nn.Conv2d(in_channels, out_channels,
                                          kernel_size=1, bias=False)

    def forward(self, x):
        x = self.depthwise_conv2d(x)
        x = self.pointwise_conv2d(x)
        return x


class BranchSeparables(nn.Module):

    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 stem_cell=False, zero_pad=False):
        super(BranchSeparables, self).__init__()
        padding = kernel_size // 2
        middle_channels = out_channels if stem_cell else in_channels
        self.zero_pad = nn.ZeroPad2d((1, 0, 1, 0)) if zero_pad else None
        self.relu_1 = nn.ReLU()
        self.separable_1 = SeparableConv2d(in_channels, middle_channels,
                                           kernel_size, dw_stride=stride,
                                           dw_padding=padding)
        self.bn_sep_1 = nn.BatchNorm2d(middle_channels, eps=0.001)
        self.relu_2 = nn.ReLU()
        self.separable_2 = SeparableConv2d(middle_channels, out_channels,
                                           kernel_size, dw_stride=1,
                                           dw_padding=padding)
        self.bn_sep_2 = nn.BatchNorm2d(out_channels, eps=0.001)

    def forward(self, x):
        x = self.relu_1(x)
        if self.zero_pad:
            x = self.zero_pad(x)
        x = self.separable_1(x)
        if self.zero_pad:
            x = x[:, :, 1:, 1:].contiguous()
        x = self.bn_sep_1(x)
        x = self.relu_2(x)
        x = self.separable_2(x)
        x = self.bn_sep_2(x)
        return x


class ReluConvBn(nn.Module):

    def __init__(self, in_channels, out_channels, kernel_size, stride=1):
        super(ReluConvBn, self).__init__()
        self.relu = nn.ReLU()
        self.conv = nn.Conv2d(in_channels, out_channels,
                              kernel_size=kernel_size, stride=stride,
                              bias=False)
        self.bn = nn.BatchNorm2d(out_channels, eps=0.001)

    def forward(self, x):
        x = self.relu(x)
        x = self.conv(x)
        x = self.bn(x)
        return x


class FactorizedReduction(nn.Module):

    def __init__(self, in_channels, out_channels):
        super(FactorizedReduction, self).__init__()
        self.relu = nn.ReLU()
        self.path_1 = nn.Sequential(OrderedDict([
            ('avgpool', nn.AvgPool2d(1, stride=2, count_include_pad=False)),
            ('conv', nn.Conv2d(in_channels, out_channels // 2,
                               kernel_size=1, bias=False)),
        ]))
        self.path_2 = nn.Sequential(OrderedDict([
            ('pad', nn.ZeroPad2d((0, 1, 0, 1))),
            ('avgpool', nn.AvgPool2d(1, stride=2, count_include_pad=False)),
            ('conv', nn.Conv2d(in_channels, out_channels // 2,
                               kernel_size=1, bias=False)),
        ]))
        self.final_path_bn = nn.BatchNorm2d(out_channels, eps=0.001)

    def forward(self, x):
        x = self.relu(x)

        x_path1 = self.path_1(x)

        x_path2 = self.path_2.pad(x)
        x_path2 = x_path2[:, :, 1:, 1:]
        x_path2 = self.path_2.avgpool(x_path2)
        x_path2 = self.path_2.conv(x_path2)

        out = self.final_path_bn(torch.cat([x_path1, x_path2], 1))
        return out


def equal_except(a, b, avoid=None):
    for i, (ai, bi) in enumerate(zip(a, b)):
        if ai != bi and (avoid is None or i != avoid):
            return False
    return True


def shrink_common(*tensors, avoid=None):
    sizes = [tuple(t.size()) for t in tensors]
    st = tuple(min(*dims) for dims in zip(*sizes))
    out_tensors = []
    for t, s in zip(tensors, sizes):
        if not equal_except(s, st, avoid):
            dest_size = list(st)
            if avoid is not None:
                dest_size[avoid] = s[avoid]
            t = t.__getitem__(list(slice(si) for si in dest_size))
        out_tensors.append(t)
    return out_tensors


def shrink_sum(*tensors):
    tensors = shrink_common(*tensors)
    return sum(tensors)

def shrink_cat(tensors, dim=1):
    tensors = shrink_common(*tensors, avoid=dim)
    return torch.cat(tensors, dim=1)


class CellBase(nn.Module):
    """
    Modified as per:
    https://github.com/facebookresearch/multigrain/blob/master/multigrain/backbones/pnasnet.py
    """
    def cell_forward(self, x_left, x_right):
        x_comb_iter_0_left = self.comb_iter_0_left(x_left)
        x_comb_iter_0_right = self.comb_iter_0_right(x_left)
        x_comb_iter_0 = shrink_sum(x_comb_iter_0_left, x_comb_iter_0_right)

        x_comb_iter_1_left = self.comb_iter_1_left(x_right)
        x_comb_iter_1_right = self.comb_iter_1_right(x_right)
        x_comb_iter_1 = shrink_sum(x_comb_iter_1_left, x_comb_iter_1_right)

        x_comb_iter_2_left = self.comb_iter_2_left(x_right)
        x_comb_iter_2_right = self.comb_iter_2_right(x_right)
        x_comb_iter_2 = shrink_sum(x_comb_iter_2_left, x_comb_iter_2_right)

        x_comb_iter_3_left = self.comb_iter_3_left(x_comb_iter_2)
        x_comb_iter_3_right = self.comb_iter_3_right(x_right)
        x_comb_iter_3 = shrink_sum(x_comb_iter_3_left, x_comb_iter_3_right)

        x_comb_iter_4_left = self.comb_iter_4_left(x_left)
        if self.comb_iter_4_right:
            x_comb_iter_4_right = self.comb_iter_4_right(x_right)
        else:
            x_comb_iter_4_right = x_right
        x_comb_iter_4 = shrink_sum(x_comb_iter_4_left, x_comb_iter_4_right)

        x_out = shrink_cat(
            [x_comb_iter_0, x_comb_iter_1, x_comb_iter_2, x_comb_iter_3,
            x_comb_iter_4], 1)
        return x_out


class CellStem0(CellBase):

    def __init__(self, in_channels_left, out_channels_left, in_channels_right,
                 out_channels_right):
        super(CellStem0, self).__init__()
        self.conv_1x1 = ReluConvBn(in_channels_right, out_channels_right,
                                   kernel_size=1)
        self.comb_iter_0_left = BranchSeparables(in_channels_left,
                                                 out_channels_left,
                                                 kernel_size=5, stride=2,
                                                 stem_cell=True)
        self.comb_iter_0_right = nn.Sequential(OrderedDict([
            ('max_pool', MaxPool(3, stride=2)),
            ('conv', nn.Conv2d(in_channels_left, out_channels_left,
                               kernel_size=1, bias=False)),
            ('bn', nn.BatchNorm2d(out_channels_left, eps=0.001)),
        ]))
        self.comb_iter_1_left = BranchSeparables(out_channels_right,
                                                 out_channels_right,
                                                 kernel_size=7, stride=2)
        self.comb_iter_1_right = MaxPool(3, stride=2)
        self.comb_iter_2_left = BranchSeparables(out_channels_right,
                                                 out_channels_right,
                                                 kernel_size=5, stride=2)
        self.comb_iter_2_right = BranchSeparables(out_channels_right,
                                                  out_channels_right,
                                                  kernel_size=3, stride=2)
        self.comb_iter_3_left = BranchSeparables(out_channels_right,
                                                 out_channels_right,
                                                 kernel_size=3)
        self.comb_iter_3_right = MaxPool(3, stride=2)
        self.comb_iter_4_left = BranchSeparables(in_channels_right,
                                                 out_channels_right,
                                                 kernel_size=3, stride=2,
                                                 stem_cell=True)
        self.comb_iter_4_right = ReluConvBn(out_channels_right,
                                            out_channels_right,
                                            kernel_size=1, stride=2)

    def forward(self, x_left):
        x_right = self.conv_1x1(x_left)
        x_out = self.cell_forward(x_left, x_right)
        return x_out


class Cell(CellBase):

    def __init__(self, in_channels_left, out_channels_left, in_channels_right,
                 out_channels_right, is_reduction=False, zero_pad=False,
                 match_prev_layer_dimensions=False):
        super(Cell, self).__init__()

        # If `is_reduction` is set to `True` stride 2 is used for
        # convolutional and pooling layers to reduce the spatial size of
        # the output of a cell approximately by a factor of 2.
        stride = 2 if is_reduction else 1

        # If `match_prev_layer_dimensions` is set to `True`
        # `FactorizedReduction` is used to reduce the spatial size
        # of the left input of a cell approximately by a factor of 2.
        self.match_prev_layer_dimensions = match_prev_layer_dimensions
        if match_prev_layer_dimensions:
            self.conv_prev_1x1 = FactorizedReduction(in_channels_left,
                                                     out_channels_left)
        else:
            self.conv_prev_1x1 = ReluConvBn(in_channels_left,
                                            out_channels_left, kernel_size=1)

        self.conv_1x1 = ReluConvBn(in_channels_right, out_channels_right,
                                   kernel_size=1)
        self.comb_iter_0_left = BranchSeparables(out_channels_left,
                                                 out_channels_left,
                                                 kernel_size=5, stride=stride,
                                                 zero_pad=zero_pad)
        self.comb_iter_0_right = MaxPool(3, stride=stride, zero_pad=zero_pad)
        self.comb_iter_1_left = BranchSeparables(out_channels_right,
                                                 out_channels_right,
                                                 kernel_size=7, stride=stride,
                                                 zero_pad=zero_pad)
        self.comb_iter_1_right = MaxPool(3, stride=stride, zero_pad=zero_pad)
        self.comb_iter_2_left = BranchSeparables(out_channels_right,
                                                 out_channels_right,
                                                 kernel_size=5, stride=stride,
                                                 zero_pad=zero_pad)
        self.comb_iter_2_right = BranchSeparables(out_channels_right,
                                                  out_channels_right,
                                                  kernel_size=3, stride=stride,
                                                  zero_pad=zero_pad)
        self.comb_iter_3_left = BranchSeparables(out_channels_right,
                                                 out_channels_right,
                                                 kernel_size=3)
        self.comb_iter_3_right = MaxPool(3, stride=stride, zero_pad=zero_pad)
        self.comb_iter_4_left = BranchSeparables(out_channels_left,
                                                 out_channels_left,
                                                 kernel_size=3, stride=stride,
                                                 zero_pad=zero_pad)
        if is_reduction:
            self.comb_iter_4_right = ReluConvBn(out_channels_right,
                                                out_channels_right,
                                                kernel_size=1, stride=stride)
        else:
            self.comb_iter_4_right = None

    def forward(self, x_left, x_right):
        x_left = self.conv_prev_1x1(x_left)
        x_right = self.conv_1x1(x_right)
        x_out = self.cell_forward(x_left, x_right)
        return x_out


class PNASNet5LargeEncoder(nn.Module):
    def __init__(self, in_channels=3, padding=1):
        super().__init__()
        self.in_channels = in_channels if isinstance(in_channels, int) else len(in_channels)
        self.rgb_channels = in_channels if isinstance(in_channels, str) else 'rgb'
        self.conv_0 = nn.Sequential(OrderedDict([
            ('conv', nn.Conv2d(self.in_channels, 96, kernel_size=3, stride=2,
                               bias=False, padding=(padding, padding))),
            ('bn', nn.BatchNorm2d(96, eps=0.001))
        ]))
        self.cell_stem_0 = CellStem0(in_channels_left=96, out_channels_left=54,
                                     in_channels_right=96,
                                     out_channels_right=54)
        self.cell_stem_1 = Cell(in_channels_left=96, out_channels_left=108,
                                in_channels_right=270, out_channels_right=108,
                                match_prev_layer_dimensions=True,
                                is_reduction=True)
        self.cell_0 = Cell(in_channels_left=270, out_channels_left=216,
                           in_channels_right=540, out_channels_right=216,
                           match_prev_layer_dimensions=True)
        self.cell_1 = Cell(in_channels_left=540, out_channels_left=216,
                           in_channels_right=1080, out_channels_right=216)
        self.cell_2 = Cell(in_channels_left=1080, out_channels_left=216,
                           in_channels_right=1080, out_channels_right=216)
        self.cell_3 = Cell(in_channels_left=1080, out_channels_left=216,
                           in_channels_right=1080, out_channels_right=216)
        self.cell_4 = Cell(in_channels_left=1080, out_channels_left=432,
                           in_channels_right=1080, out_channels_right=432,
                           is_reduction=True, zero_pad=True)
        self.cell_5 = Cell(in_channels_left=1080, out_channels_left=432,
                           in_channels_right=2160, out_channels_right=432,
                           match_prev_layer_dimensions=True)
        self.cell_6 = Cell(in_channels_left=2160, out_channels_left=432,
                           in_channels_right=2160, out_channels_right=432)
        self.cell_7 = Cell(in_channels_left=2160, out_channels_left=432,
                           in_channels_right=2160, out_channels_right=432)
        self.cell_8 = Cell(in_channels_left=2160, out_channels_left=864,
                           in_channels_right=2160, out_channels_right=864,
                           is_reduction=True)
        self.cell_9 = Cell(in_channels_left=2160, out_channels_left=864,
                           in_channels_right=4320, out_channels_right=864,
                           match_prev_layer_dimensions=True)
        self.cell_10 = Cell(in_channels_left=4320, out_channels_left=864,
                            in_channels_right=4320, out_channels_right=864)
        self.cell_11 = Cell(in_channels_left=4320, out_channels_left=864,
                            in_channels_right=4320, out_channels_right=864)

    def forward(self, x):
        skips = []
        x_conv_0 = self.conv_0(x)  # downsize
        skips.append(x_conv_0)

        x_stem_0 = self.cell_stem_0(x_conv_0)  # downsize
        skips.append(x_stem_0)

        x_stem_1 = self.cell_stem_1(x_conv_0, x_stem_0)  # downsize
        x_cell_0 = self.cell_0(x_stem_0, x_stem_1)
        x_cell_1 = self.cell_1(x_stem_1, x_cell_0)
        x_cell_2 = self.cell_2(x_cell_0, x_cell_1)
        x_cell_3 = self.cell_3(x_cell_1, x_cell_2)
        skips.append(x_cell_3)

        x_cell_4 = self.cell_4(x_cell_2, x_cell_3)  # downsize
        x_cell_5 = self.cell_5(x_cell_3, x_cell_4)
        x_cell_6 = self.cell_6(x_cell_4, x_cell_5)
        x_cell_7 = self.cell_7(x_cell_5, x_cell_6)
        skips.append(x_cell_7)

        x_cell_8 = self.cell_8(x_cell_6, x_cell_7)  # downsize
        x_cell_9 = self.cell_9(x_cell_7, x_cell_8)
        x_cell_10 = self.cell_10(x_cell_8, x_cell_9)
        x_cell_11 = self.cell_11(x_cell_9, x_cell_10)
        skips.append(x_cell_11)
        return list(reversed(skips))

    def load_state_dict(self, state_dict, **kwargs):
        state_dict.pop('last_linear.bias')
        state_dict.pop('last_linear.weight')
        if self.in_channels != 3:
            state_dict = self.modify_in_channel_weights(state_dict, self.rgb_channels)
        super().load_state_dict(state_dict, **kwargs)

    def modify_in_channel_weights(self, state_dict, rgb_channels):
        pretrained = state_dict['conv_0.conv.weight']
        cycled_weights = select_rgb_weights(pretrained, rgb_channels)
        state_dict['conv_0.conv.weight'] = cycled_weights
        return state_dict


pnasnet_encoders = {
    'pnasnet-5large': {
        'encoder': PNASNet5LargeEncoder,
        'pretrained_settings': {
            'imagenet': {
                'url': 'http://data.lip6.fr/cadene/pretrainedmodels/pnasnet5large-bf079911.pth',
                'input_space': 'RGB',
                'input_size': [3, 331, 331],
                'input_range': [0, 1],
                'mean': [0.5, 0.5, 0.5],
                'std': [0.5, 0.5, 0.5],
                'num_classes': 1000
            },
            'imagenet+background': {
                'url': 'http://data.lip6.fr/cadene/pretrainedmodels/pnasnet5large-bf079911.pth',
                'input_space': 'RGB',
                'input_size': [3, 331, 331],
                'input_range': [0, 1],
                'mean': [0.5, 0.5, 0.5],
                'std': [0.5, 0.5, 0.5],
                'num_classes': 1001
            }
        },
        'out_shapes': (4320, 2160, 1080, 270, 96),
        'params': {'padding': 1}
    }
}