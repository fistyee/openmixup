# reference: https://github.com/open-mmlab/mmclassification/tree/master/mmcls/models/backbones
# modified from mmclassification resnet.py, resnet_cifar.py
import random
import torch
import torch.nn as nn
import torch.utils.checkpoint as cp
from mmcv.cnn.bricks import DropPath
from mmcv.cnn import (ConvModule, build_conv_layer, build_norm_layer,
                      constant_init, kaiming_init)
from mmcv.utils.parrots_wrapper import _BatchNorm

from ..registry import BACKBONES
from .base_backbone import BaseBackbone
from ..utils import grad_batch_shuffle_ddp, grad_batch_unshuffle_ddp


class BasicBlock(nn.Module):
    """BasicBlock for ResNet.

    Args:
        in_channels (int): Input channels of this block.
        out_channels (int): Output channels of this block.
        expansion (int): The ratio of ``out_channels/mid_channels`` where
            ``mid_channels`` is the output channels of conv1. This is a
            reserved argument in BasicBlock and should always be 1. Default: 1.
        stride (int): stride of the block. Default: 1
        dilation (int): dilation of convolution. Default: 1
        downsample (nn.Module): downsample operation on identity branch.
            Default: None.
        style (str): `pytorch` or `caffe`. It is unused and reserved for
            unified API with Bottleneck.
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
            memory while slowing down the training speed.
        conv_cfg (dict): dictionary to construct and config conv layer.
            Default: None
        norm_cfg (dict): dictionary to construct and config norm layer.
            Default: dict(type='BN')
        drop_path_rate (float): Additional Stochastic DropPath in each basic_block
            for RSB A1/A2. Default to 0.
    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 expansion=1,
                 stride=1,
                 dilation=1,
                 downsample=None,
                 style='pytorch',
                 with_cp=False,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN'),
                 drop_path_rate=0.0,
                 **kwargs):
        super(BasicBlock, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.expansion = expansion
        assert self.expansion == 1
        assert out_channels % expansion == 0
        self.mid_channels = out_channels // expansion
        self.stride = stride
        self.dilation = dilation
        self.style = style
        self.with_cp = with_cp
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg

        self.norm1_name, norm1 = build_norm_layer(
            norm_cfg, self.mid_channels, postfix=1)
        self.norm2_name, norm2 = build_norm_layer(
            norm_cfg, out_channels, postfix=2)

        self.conv1 = build_conv_layer(
            conv_cfg,
            in_channels,
            self.mid_channels,
            3,
            stride=stride,
            padding=dilation,
            dilation=dilation,
            bias=False)
        self.add_module(self.norm1_name, norm1)
        self.conv2 = build_conv_layer(
            conv_cfg,
            self.mid_channels,
            out_channels,
            3,
            padding=1,
            bias=False)
        self.add_module(self.norm2_name, norm2)

        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.drop_path = DropPath(drop_prob=drop_path_rate) \
            if drop_path_rate > 1e-6 else nn.Identity()

    @property
    def norm1(self):
        return getattr(self, self.norm1_name)

    @property
    def norm2(self):
        return getattr(self, self.norm2_name)

    def forward(self, x):

        def _inner_forward(x):
            identity = x

            out = self.conv1(x)
            out = self.norm1(out)
            out = self.relu(out)

            out = self.conv2(out)
            out = self.norm2(out)

            if self.downsample is not None:
                identity = self.downsample(x)
            
            out = self.drop_path(out)
            out += identity

            return out

        if self.with_cp and x.requires_grad:
            out = cp.checkpoint(_inner_forward, x)
        else:
            out = _inner_forward(x)

        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    """Bottleneck block for ResNet.

    Args:
        in_channels (int): Input channels of this block.
        out_channels (int): Output channels of this block.
        expansion (int): The ratio of ``out_channels/mid_channels`` where
            ``mid_channels`` is the input/output channels of conv2. Default: 4.
        stride (int): stride of the block. Default: 1
        dilation (int): dilation of convolution. Default: 1
        downsample (nn.Module): downsample operation on identity branch.
            Default: None.
        style (str): ``"pytorch"`` or ``"caffe"``. If set to "pytorch", the
            stride-two layer is the 3x3 conv layer, otherwise the stride-two
            layer is the first 1x1 conv layer. Default: "pytorch".
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
            memory while slowing down the training speed.
        conv_cfg (dict): dictionary to construct and config conv layer.
            Default: None
        norm_cfg (dict): dictionary to construct and config norm layer.
            Default: dict(type='BN')
        drop_path_rate (float): Additional Stochastic DropPath in each residual_block
            for RSB A1/A2. Default to 0.
    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 expansion=4,
                 stride=1,
                 dilation=1,
                 downsample=None,
                 style='pytorch',
                 with_cp=False,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN'),
                 drop_path_rate=0.0,
                 **kwargs):
        super(Bottleneck, self).__init__()
        assert style in ['pytorch', 'caffe']

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.expansion = expansion
        assert out_channels % expansion == 0
        self.mid_channels = out_channels // expansion
        self.stride = stride
        self.dilation = dilation
        self.style = style
        self.with_cp = with_cp
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg

        if self.style == 'pytorch':
            self.conv1_stride = 1
            self.conv2_stride = stride
        else:
            self.conv1_stride = stride
            self.conv2_stride = 1

        self.norm1_name, norm1 = build_norm_layer(
            norm_cfg, self.mid_channels, postfix=1)
        self.norm2_name, norm2 = build_norm_layer(
            norm_cfg, self.mid_channels, postfix=2)
        self.norm3_name, norm3 = build_norm_layer(
            norm_cfg, out_channels, postfix=3)

        self.conv1 = build_conv_layer(
            conv_cfg,
            in_channels,
            self.mid_channels,
            kernel_size=1,
            stride=self.conv1_stride,
            bias=False)
        self.add_module(self.norm1_name, norm1)
        self.conv2 = build_conv_layer(
            conv_cfg,
            self.mid_channels,
            self.mid_channels,
            kernel_size=3,
            stride=self.conv2_stride,
            padding=dilation,
            dilation=dilation,
            bias=False)

        self.add_module(self.norm2_name, norm2)
        self.conv3 = build_conv_layer(
            conv_cfg,
            self.mid_channels,
            out_channels,
            kernel_size=1,
            bias=False)
        self.add_module(self.norm3_name, norm3)

        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.drop_path = DropPath(drop_prob=drop_path_rate) \
            if drop_path_rate > 1e-6 else nn.Identity()

    @property
    def norm1(self):
        return getattr(self, self.norm1_name)

    @property
    def norm2(self):
        return getattr(self, self.norm2_name)

    @property
    def norm3(self):
        return getattr(self, self.norm3_name)

    def forward(self, x):

        def _inner_forward(x):
            identity = x

            out = self.conv1(x)
            out = self.norm1(out)
            out = self.relu(out)

            out = self.conv2(out)
            out = self.norm2(out)
            out = self.relu(out)

            out = self.conv3(out)
            out = self.norm3(out)

            if self.downsample is not None:
                identity = self.downsample(x)

            out = self.drop_path(out)
            out += identity

            return out

        if self.with_cp and x.requires_grad:
            out = cp.checkpoint(_inner_forward, x)
        else:
            out = _inner_forward(x)

        out = self.relu(out)

        return out


def get_expansion(block, expansion=None):
    """Get the expansion of a residual block.

    The block expansion will be obtained by the following order:

    1. If ``expansion`` is given, just return it.
    2. If ``block`` has the attribute ``expansion``, then return
       ``block.expansion``.
    3. Return the default value according the the block type:
       1 for ``BasicBlock`` and 4 for ``Bottleneck``.

    Args:
        block (class): The block class.
        expansion (int | None): The given expansion ratio.

    Returns:
        int: The expansion of the block.
    """
    if isinstance(expansion, int):
        assert expansion > 0
    elif expansion is None:
        if hasattr(block, 'expansion'):
            expansion = block.expansion
        elif issubclass(block, BasicBlock):
            expansion = 1
        elif issubclass(block, Bottleneck):
            expansion = 4
        else:
            raise TypeError(f'expansion is not specified for {block.__name__}')
    else:
        raise TypeError('expansion must be an integer or None')

    return expansion


class ResLayer(nn.Sequential):
    """ResLayer to build ResNet style backbone.

    Args:
        block (nn.Module): Residual block used to build ResLayer.
        num_blocks (int): Number of blocks.
        in_channels (int): Input channels of this block.
        out_channels (int): Output channels of this block.
        expansion (int, optional): The expansion for BasicBlock/Bottleneck.
            If not specified, it will firstly be obtained via
            ``block.expansion``. If the block has no attribute "expansion",
            the following default values will be used: 1 for BasicBlock and
            4 for Bottleneck. Default: None.
        stride (int): stride of the first block. Default: 1.
        avg_down (bool): Use AvgPool instead of stride conv when
            downsampling in the bottleneck. Default: False
        conv_cfg (dict): dictionary to construct and config conv layer.
            Default: None
        norm_cfg (dict): dictionary to construct and config norm layer.
            Default: dict(type='BN').
        drop_path_rate (float | list): Additional Stochastic DropPath in each
            block for RSB A1/A2. Default to 0.
    """

    def __init__(self,
                 block,
                 num_blocks,
                 in_channels,
                 out_channels,
                 expansion=None,
                 stride=1,
                 avg_down=False,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN'),
                 drop_path_rate=0.0,
                 **kwargs):
        self.block = block
        self.expansion = get_expansion(block, expansion)

        downsample = None
        if stride != 1 or in_channels != out_channels:
            downsample = []
            conv_stride = stride
            if avg_down and stride != 1:
                conv_stride = 1
                downsample.append(
                    nn.AvgPool2d(
                        kernel_size=stride,
                        stride=stride,
                        ceil_mode=True,
                        count_include_pad=False))
            downsample.extend([
                build_conv_layer(
                    conv_cfg,
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    stride=conv_stride,
                    bias=False),
                build_norm_layer(norm_cfg, out_channels)[1]
            ])
            downsample = nn.Sequential(*downsample)

        if isinstance(drop_path_rate, float):
            drop_path_rate = [drop_path_rate for _ in range(num_blocks)]

        layers = []
        layers.append(
            block(
                in_channels=in_channels,
                out_channels=out_channels,
                expansion=self.expansion,
                stride=stride,
                downsample=downsample,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                drop_path_rate=drop_path_rate[0],
                **kwargs))
        in_channels = out_channels
        for i in range(1, num_blocks):
            layers.append(
                block(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    expansion=self.expansion,
                    stride=1,
                    conv_cfg=conv_cfg,
                    norm_cfg=norm_cfg,
                    drop_path_rate=drop_path_rate[i],
                    **kwargs))
        super(ResLayer, self).__init__(*layers)


@BACKBONES.register_module()
class ResNet(BaseBackbone):
    """ResNet backbone.

    Please refer to the `paper <https://arxiv.org/abs/1512.03385>`_ for
    details.

    Args:
        depth (int): Network depth, from {18, 34, 50, 101, 152}.
        in_channels (int): Number of input image channels. Default: 3.
        stem_channels (int): Output channels of the stem layer. Default: 64.
        base_channels (int): Middle channels of the first stage. Default: 64.
        num_stages (int): Stages of the network. Default: 4.
        strides (Sequence[int]): Strides of the first block of each stage.
            Default: ``(1, 2, 2, 2)``.
        dilations (Sequence[int]): Dilation of each stage.
            Default: ``(1, 1, 1, 1)``.
        out_indices (Sequence[int]): Output from which stages. If only one
            stage is specified, a single tensor (feature map) is returned,
            otherwise multiple stages are specified, a tuple of tensors will
            be returned. Default: ``(0, 1, 2, 3,)``.
        style (str): `pytorch` or `caffe`. If set to "pytorch", the stride-two
            layer is the 3x3 conv layer, otherwise the stride-two layer is
            the first 1x1 conv layer.
        deep_stem (bool): Replace 7x7 conv in input stem with 3 3x3 conv.
            Default: False.
        avg_down (bool): Use AvgPool instead of stride conv when
            downsampling in the bottleneck. Default: False.
        frozen_stages (int): Stages to be frozen (stop grad and set eval mode).
            -1 means not freezing any parameters. Default: -1.
        conv_cfg (dict | None): The config dict for conv layers. Default: None.
        norm_cfg (dict): The config dict for norm layers.
        norm_eval (bool): Whether to set norm layers to eval mode, namely,
            freeze running stats (mean and var). Note: Effect on Batch Norm
            and its variants only. Default: False.
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
            memory while slowing down the training speed. Default: False.
        zero_init_residual (bool): Whether to use zero init for last norm layer
            in resblocks to let them behave as identity. Default: True.

    Example:
        >>> from mmcls.models import ResNet
        >>> import torch
        >>> self = ResNet(depth=18)
        >>> self.eval()
        >>> inputs = torch.rand(1, 3, 32, 32)
        >>> level_outputs = self.forward(inputs)
        >>> for level_out in level_outputs:
        ...     print(tuple(level_out.shape))
        (1, 64, 8, 8)
        (1, 128, 4, 4)
        (1, 256, 2, 2)
        (1, 512, 1, 1)
    """

    arch_settings = {
        18: (BasicBlock, (2, 2, 2, 2)),
        34: (BasicBlock, (3, 4, 6, 3)),
        50: (Bottleneck, (3, 4, 6, 3)),
        101: (Bottleneck, (3, 4, 23, 3)),
        152: (Bottleneck, (3, 8, 36, 3)),
        200: (Bottleneck, (3, 24, 36, 3)),
        269: (Bottleneck, (3, 30, 48, 8))
    }

    def __init__(self,
                 depth,
                 in_channels=3,
                 stem_channels=64,
                 base_channels=64,
                 expansion=None,
                 num_stages=4,
                 strides=(1, 2, 2, 2),
                 dilations=(1, 1, 1, 1),
                 out_indices=(0, 1, 2, 3,),
                 style='pytorch',
                 deep_stem=False,
                 avg_down=False,
                 frozen_stages=-1,
                 conv_cfg=None,
                 norm_cfg=dict(type='BN', requires_grad=True),
                 norm_eval=False,
                 with_cp=False,
                 zero_init_residual=True,
                 drop_path_rate=0.0,
                 init_cfg=None):
        super(ResNet, self).__init__(init_cfg)
        if depth not in self.arch_settings:
            raise KeyError(f'invalid depth {depth} for resnet')
        self.depth = depth
        self.stem_channels = stem_channels
        self.base_channels = base_channels
        self.num_stages = num_stages
        assert num_stages >= 1 and num_stages <= 4
        self.strides = strides
        self.dilations = dilations
        assert len(strides) == len(dilations) == num_stages
        self.out_indices = out_indices
        assert max(out_indices) < num_stages
        self.style = style
        self.deep_stem = deep_stem
        self.avg_down = avg_down
        self.frozen_stages = frozen_stages
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.with_cp = with_cp
        self.norm_eval = norm_eval
        self.zero_init_residual = zero_init_residual
        self.block, stage_blocks = self.arch_settings[depth]
        self.stage_blocks = stage_blocks[:num_stages]
        self.expansion = get_expansion(self.block, expansion)

        self._make_stem_layer(in_channels, stem_channels)

        # stochastic depth
        total_block = sum(self.stage_blocks)
        block_dpr = [
            x.item() for x in torch.linspace(0, drop_path_rate, total_block)
        ]  # stochastic depth linear decay rule

        self.res_layers = []
        _in_channels = stem_channels
        _out_channels = base_channels * self.expansion
        for i, num_blocks in enumerate(self.stage_blocks):
            stride = strides[i]
            dilation = dilations[i]
            res_layer = self.make_res_layer(
                block=self.block,
                num_blocks=num_blocks,
                in_channels=_in_channels,
                out_channels=_out_channels,
                expansion=self.expansion,
                stride=stride,
                dilation=dilation,
                style=self.style,
                avg_down=self.avg_down,
                with_cp=with_cp,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                drop_path_rate=block_dpr[:num_blocks])
            _in_channels = _out_channels
            _out_channels *= 2
            layer_name = f'layer{i + 1}'
            block_dpr = block_dpr[num_blocks:]
            self.add_module(layer_name, res_layer)
            self.res_layers.append(layer_name)

        self._freeze_stages()

        self.feat_dim = res_layer[-1].out_channels

    def make_res_layer(self, **kwargs):
        return ResLayer(**kwargs)

    @property
    def norm1(self):
        return getattr(self, self.norm1_name)

    def _make_stem_layer(self, in_channels, stem_channels):
        if self.deep_stem:
            self.stem = nn.Sequential(
                ConvModule(
                    in_channels,
                    stem_channels // 2,
                    kernel_size=3,
                    stride=2,
                    padding=1,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg,
                    inplace=True),
                ConvModule(
                    stem_channels // 2,
                    stem_channels // 2,
                    kernel_size=3,
                    stride=1,
                    padding=1,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg,
                    inplace=True),
                ConvModule(
                    stem_channels // 2,
                    stem_channels,
                    kernel_size=3,
                    stride=1,
                    padding=1,
                    conv_cfg=self.conv_cfg,
                    norm_cfg=self.norm_cfg,
                    inplace=True))
        else:
            self.conv1 = build_conv_layer(
                self.conv_cfg,
                in_channels,
                stem_channels,
                kernel_size=7,
                stride=2,
                padding=3,
                bias=False)
            self.norm1_name, norm1 = build_norm_layer(
                self.norm_cfg, stem_channels, postfix=1)
            self.add_module(self.norm1_name, norm1)
            self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

    def _freeze_stages(self):
        if self.frozen_stages >= 0:
            if self.deep_stem:
                # self.stem.eval()
                for param in self.stem.parameters():
                    param.requires_grad = False
            else:
                # self.norm1.eval()
                for m in [self.conv1, self.norm1]:
                    for param in m.parameters():
                        param.requires_grad = False

        for i in range(1, self.frozen_stages + 1):
            m = getattr(self, f'layer{i}')
            # m.eval()
            for param in m.parameters():
                param.requires_grad = False
    
    def _freeze_bn(self):
        """ keep normalization layer freezed. """
        for m in self.modules():
            # trick: eval have effect on BatchNorm only
            if isinstance(m, (_BatchNorm, nn.SyncBatchNorm)):
                m.eval()

    def _unfreeze_bn(self):
        for m in self.modules():
            if isinstance(m, (_BatchNorm, nn.SyncBatchNorm)):
                m.train()

    def init_weights(self, pretrained=None):
        super(ResNet, self).init_weights(pretrained)

        if pretrained is None:
            for m in self.modules():
                if isinstance(m, nn.Conv2d):
                    kaiming_init(m)
                elif isinstance(m, (_BatchNorm, nn.GroupNorm, nn.SyncBatchNorm)):
                    constant_init(m, val=1, bias=0)

            if self.zero_init_residual:
                for m in self.modules():
                    if isinstance(m, Bottleneck):
                        constant_init(m.norm3, 0)
                    elif isinstance(m, BasicBlock):
                        constant_init(m.norm2, 0)

    def forward(self, x):
        if self.deep_stem:
            x = self.stem(x)
        else:
            x = self.relu(self.norm1(self.conv1(x)))
        x = self.maxpool(x)
        outs = []
        for i, layer_name in enumerate(self.res_layers):
            res_layer = getattr(self, layer_name)
            x = res_layer(x)
            if i in self.out_indices:
                outs.append(x)
                if len(self.out_indices) == 1:
                    return outs
        return outs

    def train(self, mode=True):
        super(ResNet, self).train(mode)
        self._freeze_stages()
        if mode and self.norm_eval:
            for m in self.modules():
                # trick: eval have effect on BatchNorm only
                if isinstance(m, (_BatchNorm, nn.SyncBatchNorm)):
                    m.eval()


@BACKBONES.register_module()
class ResNetV1d(ResNet):
    """ResNetV1d variant described in
    `Bag of Tricks <https://arxiv.org/pdf/1812.01187.pdf>`_.

    Compared with default ResNet(ResNetV1b), ResNetV1d replaces the 7x7 conv
    in the input stem with three 3x3 convs. And in the downsampling block,
    a 2x2 avg_pool with stride 2 is added before conv, whose stride is
    changed to 1.
    """

    def __init__(self, **kwargs):
        super(ResNetV1d, self).__init__(
            deep_stem=True, avg_down=True, **kwargs)


@BACKBONES.register_module()
class ResNet_CIFAR(ResNet):
    """ResNet backbone for CIFAR.

    Compared to standard ResNet, it uses `kernel_size=3` and `stride=1` in
    conv1, and does not apply MaxPoolinng after stem. It has been proven to
    be more efficient than standard ResNet in other public codebase, e.g.,
    `https://github.com/kuangliu/pytorch-cifar/blob/master/models/resnet.py`.

    Args:
        depth (int): Network depth, from {18, 34, 50, 101, 152}.
        in_channels (int): Number of input image channels. Default: 3.
        stem_channels (int): Output channels of the stem layer. Default: 64.
        base_channels (int): Middle channels of the first stage. Default: 64.
        num_stages (int): Stages of the network. Default: 4.
        strides (Sequence[int]): Strides of the first block of each stage.
            Default: ``(1, 2, 2, 2)``.
        dilations (Sequence[int]): Dilation of each stage.
            Default: ``(1, 1, 1, 1)``.
        out_indices (Sequence[int]): Output from which stages. If only one
            stage is specified, a single tensor (feature map) is returned,
            otherwise multiple stages are specified, a tuple of tensors will
            be returned. Default: ``(3, )``.
        style (str): `pytorch` or `caffe`. If set to "pytorch", the stride-two
            layer is the 3x3 conv layer, otherwise the stride-two layer is
            the first 1x1 conv layer.
        deep_stem (bool): This network has specific designed stem, thus it is
            asserted to be False.
        avg_down (bool): Use AvgPool instead of stride conv when
            downsampling in the bottleneck. Default: False.
        frozen_stages (int): Stages to be frozen (stop grad and set eval mode).
            -1 means not freezing any parameters. Default: -1.
        conv_cfg (dict | None): The config dict for conv layers. Default: None.
        norm_cfg (dict): The config dict for norm layers.
        norm_eval (bool): Whether to set norm layers to eval mode, namely,
            freeze running stats (mean and var). Note: Effect on Batch Norm
            and its variants only. Default: False.
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
            memory while slowing down the training speed. Default: False.
        zero_init_residual (bool): Whether to use zero init for last norm layer
            in resblocks to let them behave as identity. Default: True.
    """

    def __init__(self, depth, deep_stem=False, **kwargs):
        super(ResNet_CIFAR, self).__init__(
            depth, deep_stem=deep_stem, **kwargs)
        assert not self.deep_stem, 'ResNet_CIFAR do not support deep_stem'

    def _make_stem_layer(self, in_channels, base_channels):
        self.conv1 = build_conv_layer(
            self.conv_cfg,
            in_channels,
            base_channels,
            kernel_size=3,
            stride=1,
            padding=1,
            bias=False)
        self.norm1_name, norm1 = build_norm_layer(
            self.norm_cfg, base_channels, postfix=1)
        self.add_module(self.norm1_name, norm1)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.relu(self.norm1(self.conv1(x)))
        outs = []
        for i, layer_name in enumerate(self.res_layers):
            res_layer = getattr(self, layer_name)
            x = res_layer(x)
            if i in self.out_indices:
                outs.append(x)
                if len(self.out_indices) == 1:
                    return outs
        return outs


@BACKBONES.register_module()
class ResNet_Mix(ResNet):
    """ResNet Support ManifoldMix and its variants
        v09.13

    Provide a port to mixup the latent space.
    """

    def __init__(self, **kwargs):
        super(ResNet_Mix, self).__init__(**kwargs)

    def _feature_mixup(self, x, mask, dist_shuffle=False, idx_shuffle_mix=None,
                       cross_view=False, BN_shuffle=False, idx_shuffle_BN=None,
                       idx_unshuffle_BN=None, **kwargs):
        """ mixup two feature maps with the pixel-wise mask
        
        Args:
            x, mask (tensor): Input x [N,C,H,W] and mixup mask [N, \*, H, W].
            dist_shuffle (bool): Whether to shuffle cross gpus.
            idx_shuffle_mix (tensor): Shuffle indice of [N,1] to generate x_.
            cross_view (bool): Whether to view the input x as two views [2N, C, H, W],
                which is usually adopted in self-supervised and semi-supervised settings.
            BN_shuffle (bool): Whether to do shuffle cross gpus for shuffle_BN.
            idx_shuffle_BN (tensor): Shuffle indice to utilize shuffle_BN cross gpus.
            idx_unshuffle_BN (tensor): Unshuffle indice for the shuffle_BN (in pair).
        """
        # adjust mixup mask
        assert mask.dim() == 4 and mask.size(1) <= 2
        if mask.size(1) == 1:
            mask = [mask, 1 - mask]
        else:
            mask = [
                mask[:, 0, :, :].unsqueeze(1), mask[:, 1, :, :].unsqueeze(1)]
        # undo shuffle_BN for ssl mixup
        if BN_shuffle:
            assert idx_unshuffle_BN is not None and idx_shuffle_BN is not None
            x = grad_batch_unshuffle_ddp(x, idx_unshuffle_BN)  # 2N index if cross_view
        
        # shuffle input
        if dist_shuffle==True:  # cross gpus shuffle
            assert idx_shuffle_mix is not None
            if cross_view:
                N = x.size(0) // 2
                detach_p = random.random()
                x_ = x[N:, ...].clone().detach() if detach_p < 0.5 else x[N:, ...]
                x = x[:N, ...] if detach_p < 0.5 else x[:N, ...].detach()
                x_, _, _ = grad_batch_shuffle_ddp(x_, idx_shuffle_mix)
            else:
                x_, _, _ = grad_batch_shuffle_ddp(x, idx_shuffle_mix)
        else:  # within each gpu
            if cross_view:
                # default: the input image is shuffled
                N = x.size(0) // 2
                detach_p = random.random()
                x_ = x[N:, ...].clone().detach() if detach_p < 0.5 else x[N:, ...]
                x = x[:N, ...] if detach_p < 0.5 else x[:N, ...].detach()
            else:
                x_ = x[idx_shuffle_mix, :]
        assert x.size(3) == mask[0].size(3), \
            "mismatching mask x={}, mask={}.".format(x.size(), mask[0].size())
        mix = x * mask[0] + x_ * mask[1]

        # redo shuffle_BN for ssl mixup
        if BN_shuffle:
            mix, _, _ = grad_batch_shuffle_ddp(mix, idx_shuffle_BN)  # N index
        
        return mix

    def forward(self, x, mix_args=None):
        """ only support mask-based mixup policy """
        # latent space mixup
        if mix_args is not None:
            assert isinstance(mix_args, dict)
            mix_layer = mix_args["layer"]  # {0, 1, 2, 3}
            if mix_args["BN_shuffle"]:
                x, _, idx_unshuffle = grad_batch_shuffle_ddp(x)  # 2N index if cross_view
            else:
                idx_unshuffle = None
        else:
            mix_layer = -1
        
        # input mixup
        if mix_layer == 0:
            x = self._feature_mixup(x, idx_unshuffle_BN=idx_unshuffle, **mix_args)
        # normal resnet stem
        if self.deep_stem:
            x = self.stem(x)
        else:
            x = self.relu(self.norm1(self.conv1(x)))
        x = self.maxpool(x)

        outs = []
        # stage 1 to 4
        for i, layer_name in enumerate(self.res_layers):
            # res block
            res_layer = getattr(self, layer_name)
            x = res_layer(x)
            if i in self.out_indices:
                outs.append(x)
                if len(self.out_indices) == 1:
                    return outs
            if i+1 == mix_layer:
                x = self._feature_mixup(x, idx_unshuffle_BN=idx_unshuffle, **mix_args)
        return outs


@BACKBONES.register_module()
class ResNet_Mix_CIFAR(ResNet):
    """ResNet backbone for CIFAR, support ManifoldMix and its variants
        v09.13

    Provide a port to mixup the latent space.
    """
    def __init__(self, depth, deep_stem=False, **kwargs):
        super(ResNet_Mix_CIFAR, self).__init__(
            depth, deep_stem=deep_stem, **kwargs)
        assert not self.deep_stem, 'ResNet_CIFAR do not support deep_stem'

    def _make_stem_layer(self, in_channels, base_channels):
        self.conv1 = build_conv_layer(
            self.conv_cfg,
            in_channels,
            base_channels,
            kernel_size=3,
            stride=1,
            padding=1,
            bias=False)
        self.norm1_name, norm1 = build_norm_layer(
            self.norm_cfg, base_channels, postfix=1)
        self.add_module(self.norm1_name, norm1)
        self.relu = nn.ReLU(inplace=True)

    def _feature_mixup(self, x, mask, dist_shuffle=False, idx_shuffle_mix=None,
                       cross_view=False, BN_shuffle=False, idx_shuffle_BN=None,
                       idx_unshuffle_BN=None, **kwargs):
        """ mixup two feature maps with the pixel-wise mask

        Args:
            x, mask (tensor): Input x [N,C,H,W] and mixup mask [N, \*, H, W].
            dist_shuffle (bool): Whether to shuffle cross gpus.
            idx_shuffle_mix (tensor): Shuffle indice of [N,1] to generate x_.
            cross_view (bool): Whether to view the input x as two views [2N, C, H, W],
                which is usually adopted in self-supervised and semi-supervised settings.
            BN_shuffle (bool): Whether to do shuffle cross gpus for shuffle_BN.
            idx_shuffle_BN (tensor): Shuffle indice to utilize shuffle_BN cross gpus.
            idx_unshuffle_BN (tensor): Unshuffle indice for the shuffle_BN (in pair).
        """
        # adjust mixup mask
        assert mask.dim() == 4 and mask.size(1) <= 2
        if mask.size(1) == 1:
            mask = [mask, 1 - mask]
        else:
            mask = [
                mask[:, 0, :, :].unsqueeze(1), mask[:, 1, :, :].unsqueeze(1)]
        # undo shuffle_BN for ssl mixup
        if BN_shuffle:
            assert idx_unshuffle_BN is not None and idx_shuffle_BN is not None
            x = grad_batch_unshuffle_ddp(x, idx_unshuffle_BN)  # 2N index if cross_view
        
        # shuffle input
        if dist_shuffle==True:  # cross gpus shuffle
            assert idx_shuffle_mix is not None
            if cross_view:
                N = x.size(0) // 2
                detach_p = random.random()
                x_ = x[N:, ...].clone().detach() if detach_p < 0.5 else x[N:, ...]
                x = x[:N, ...] if detach_p < 0.5 else x[:N, ...].detach()
                x_, _, _ = grad_batch_shuffle_ddp(x_, idx_shuffle_mix)
            else:
                x_, _, _ = grad_batch_shuffle_ddp(x, idx_shuffle_mix)
        else:  # within each gpu
            if cross_view:
                # default: the input image is shuffled
                N = x.size(0) // 2
                detach_p = random.random()
                x_ = x[N:, ...].clone().detach() if detach_p < 0.5 else x[N:, ...]
                x = x[:N, ...] if detach_p < 0.5 else x[:N, ...].detach()
            else:
                x_ = x[idx_shuffle_mix, :]
        assert x.size(3) == mask[0].size(3), \
            "mismatching mask x={}, mask={}.".format(x.size(), mask[0].size())
        mix = x * mask[0] + x_ * mask[1]

        # redo shuffle_BN for ssl mixup
        if BN_shuffle:
            mix, _, _ = grad_batch_shuffle_ddp(mix, idx_shuffle_BN)  # N index
        
        return mix

    def forward(self, x, mix_args=None):
        """ only support mask-based mixup policy """
        # latent space mixup
        if mix_args is not None:
            assert isinstance(mix_args, dict)
            mix_layer = mix_args["layer"]  # {0, 1, 2, 3}
            if mix_args["BN_shuffle"]:
                x, _, idx_unshuffle = grad_batch_shuffle_ddp(x)  # 2N index if cross_view
            else:
                idx_unshuffle = None
        else:
            mix_layer = -1
        
        # input mixup
        if mix_layer == 0:
            x = self._feature_mixup(x, idx_unshuffle_BN=idx_unshuffle, **mix_args)
        # CIFAR stem
        x = self.relu(self.norm1(self.conv1(x)))

        outs = []
        # stage 1 to 4
        for i, layer_name in enumerate(self.res_layers):
            # res block
            res_layer = getattr(self, layer_name)
            x = res_layer(x)
            if i in self.out_indices:
                outs.append(x)
                if len(self.out_indices) == 1:
                    return outs
            if i+1 == mix_layer:
                x = self._feature_mixup(x, idx_unshuffle_BN=idx_unshuffle, **mix_args)
        return outs
