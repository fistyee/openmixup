import re
from typing import Optional, Tuple

import torch
import torch.nn as nn
from mmcv.cnn.utils.weight_init import trunc_normal_

from ..registry import BACKBONES
from ..utils import (SparseAvgPooling, SparseBatchNorm2d,
                     SparseConv2d, SparseMaxPooling, SparseSyncBatchNorm2d)
from .resnet_mmcls import ResNet


@BACKBONES.register_module()
class MIMResNet(ResNet):
    """ResNet backbone for MIM pre-training.

    Args:
        mask_layer (int): Layer to start MIM (mask img and add mask_token).
            Defaults to 0.
        mask_token (str): Mode of applying mask token in {None, 'randn', 'zero',
            'learnable', 'mean'}. Defaults to 'learnable'.
        mask_init (float): The init values of mask_token gamma. Defaults to 0.0.
    """

    def __init__(self,
                 mask_layer=0,
                 mask_token='learnable',
                 mask_init=0,
                 replace=True,
                 detach=False,
                 **kwargs):
        super(MIMResNet, self).__init__(**kwargs)
        self.mask_layer = mask_layer
        self.mask_mode = mask_token
        self.replace = replace
        self.detach = detach
        assert self.mask_layer in [0, 1, 2, 3]
        assert self.mask_mode in [
            None, 'randn', 'zero', 'mean', 'instance_mean', 'learnable',]
        ARCH_DIMS = {
            **dict.fromkeys(
                ['18', '34'],
                [64, 128, 256, 512,]),
            **dict.fromkeys(
                ['50', '101', '152', '200'],
                [64, 256, 512, 1024,]),
        }
        self.mask_dims = ARCH_DIMS[str(self.depth)][self.mask_layer]
        if self.mask_mode not in [None, 'instance_mean',]:
            self.mask_token = nn.Parameter(torch.zeros(1, self.mask_dims, 1, 1))
        if mask_init > 0 and not replace:
            self.mask_gamma = nn.Parameter(
                mask_init * torch.ones((1, self.mask_dims, 1, 1)), requires_grad=True)
        else:
            self.mask_gamma = None

    def init_weights(self, pretrained=None):
        """Initialize weights."""
        super(MIMResNet, self).init_weights(pretrained)

        if pretrained is None:
            if self.mask_mode not in [None, 'instance_mean',]:
                if self.mask_mode != 'zero':
                    trunc_normal_(self.mask_token, mean=0, std=.02)
                if self.mask_mode != 'learnable':
                    self.mask_token.requires_grad = False

    def forward_mask(self, x, mask=None):
        """ perform MIM with mask and mask_token """
        B, _, H, W = x.size()
        if self.mask_mode is None:
            return x
        elif self.mask_mode == 'instance_mean':
            mask_token = x.mean(dim=[2, 3], keepdim=True).expand(B, -1, H, W)
        else:
            if self.mask_mode == 'mean':
                self.mask_token.data = x.mean(dim=[0, 2, 3], keepdim=True)
            mask_token = self.mask_token.expand(B, -1, H, W)
        assert mask is not None
        mask = mask.view(B, 1, H, W).type_as(mask_token)
        if self.replace:
            x = x * (1. - mask) + mask_token * mask
        else:
            if self.detach:
                x = x * (1. - mask) + x.clone().detach() * mask
            if self.mask_gamma is not None:
                x = x * (1. - mask) + (x * mask) * self.mask_gamma
            x = x + mask_token * mask  # residual
        return x

    def forward(self, x, mask=None):
        # stem
        if self.deep_stem:
            x = self.stem(x)
        else:
            x = self.relu(self.norm1(self.conv1(x)))
        x = self.maxpool(x)
        
        outs = []
        if -1 in self.out_indices:
            outs.append(x)
        
        # stages
        for i, layer_name in enumerate(self.res_layers):
            # mask, add mask token
            if self.mask_layer == i:
                x = self.forward_mask(x, mask)
            
            res_layer = getattr(self, layer_name)
            x = res_layer(x)
            if i in self.out_indices:
                outs.append(x)
                if len(self.out_indices) == 1:
                    return outs
        return outs


@BACKBONES.register_module()
class SparseResNet(ResNet):
    """ResNet with sparse module conversion function.

    Modified from https://github.com/keyu-tian/SparK/blob/main/encoder.py

    Args:
        depth (int): Network depth, from {18, 34, 50, 101, 152}.
        in_channels (int): Number of input image channels. Defaults to 3.
        stem_channels (int): Output channels of the stem layer. Defaults to 64.
        base_channels (int): Middle channels of the first stage.
            Defaults to 64.
        num_stages (int): Stages of the network. Defaults to 4.
        strides (Sequence[int]): Strides of the first block of each stage.
            Defaults to ``(1, 2, 2, 2)``.
        dilations (Sequence[int]): Dilation of each stage.
            Defaults to ``(1, 1, 1, 1)``.
        out_indices (Sequence[int]): Output from which stages.
            Defaults to ``(3, )``.
        style (str): `pytorch` or `caffe`. If set to "pytorch", the stride-two
            layer is the 3x3 conv layer, otherwise the stride-two layer is
            the first 1x1 conv layer.
        deep_stem (bool): Replace 7x7 conv in input stem with 3 3x3 conv.
            Defaults to False.
        avg_down (bool): Use AvgPool instead of stride conv when
            downsampling in the bottleneck. Defaults to False.
        frozen_stages (int): Stages to be frozen (stop grad and set eval mode).
            -1 means not freezing any parameters. Defaults to -1.
        conv_cfg (dict | None): The config dict for conv layers.
            Defaults to None.
        norm_cfg (dict): The config dict for norm layers.
        norm_eval (bool): Whether to set norm layers to eval mode, namely,
            freeze running stats (mean and var). Note: Effect on Batch Norm
            and its variants only. Defaults to False.
        with_cp (bool): Use checkpoint or not. Using checkpoint will save some
            memory while slowing down the training speed. Defaults to False.
        zero_init_residual (bool): Whether to use zero init for last norm layer
            in resblocks to let them behave as identity. Defaults to True.
        drop_path_rate (float): stochastic depth rate. Defaults to 0.
    """

    def __init__(self,
                 depth: int,
                 in_channels: int = 3,
                 stem_channels: int = 64,
                 base_channels: int = 64,
                 expansion: Optional[int] = None,
                 num_stages: int = 4,
                 strides: Tuple[int] = (1, 2, 2, 2),
                 dilations: Tuple[int] = (1, 1, 1, 1),
                 out_indices: Tuple[int] = (3, ),
                 style: str = 'pytorch',
                 deep_stem: bool = False,
                 avg_down: bool = False,
                 frozen_stages: int = -1,
                 conv_cfg: Optional[dict] = None,
                 norm_cfg: dict = dict(type='SparseSyncBatchNorm2d'),
                 norm_eval: bool = False,
                 with_cp: bool = False,
                 zero_init_residual: bool = False,
                 init_cfg: Optional[dict] = [
                     dict(type='Kaiming', layer=['Conv2d']),
                     dict(
                         type='Constant',
                         val=1, layer=['_BatchNorm', 'GroupNorm'])
                 ],
                 drop_path_rate: float = 0,
                 **kwargs):
        super().__init__(
            depth=depth,
            in_channels=in_channels,
            stem_channels=stem_channels,
            base_channels=base_channels,
            expansion=expansion,
            num_stages=num_stages,
            strides=strides,
            dilations=dilations,
            out_indices=out_indices,
            style=style,
            deep_stem=deep_stem,
            avg_down=avg_down,
            frozen_stages=frozen_stages,
            conv_cfg=conv_cfg,
            norm_cfg=norm_cfg,
            norm_eval=norm_eval,
            with_cp=with_cp,
            zero_init_residual=zero_init_residual,
            init_cfg=init_cfg,
            drop_path_rate=drop_path_rate,
            **kwargs)
        norm_type = norm_cfg['type']
        enable_sync_bn = False
        if re.search('Sync', norm_type) is not None:
            enable_sync_bn = True
        self.dense_model_to_sparse(m=self, enable_sync_bn=enable_sync_bn)

    def dense_model_to_sparse(self, m: nn.Module,
                              enable_sync_bn: bool) -> nn.Module:
        """Convert regular dense modules to sparse modules."""
        output = m
        if isinstance(m, nn.Conv2d):
            m: nn.Conv2d
            bias = m.bias is not None
            output = SparseConv2d(
                m.in_channels,
                m.out_channels,
                kernel_size=m.kernel_size,
                stride=m.stride,
                padding=m.padding,
                dilation=m.dilation,
                groups=m.groups,
                bias=bias,
                padding_mode=m.padding_mode,
            )
            output.weight.data.copy_(m.weight.data)
            if bias:
                output.bias.data.copy_(m.bias.data)

        elif isinstance(m, nn.MaxPool2d):
            m: nn.MaxPool2d
            output = SparseMaxPooling(
                m.kernel_size,
                stride=m.stride,
                padding=m.padding,
                dilation=m.dilation,
                return_indices=m.return_indices,
                ceil_mode=m.ceil_mode)

        elif isinstance(m, nn.AvgPool2d):
            m: nn.AvgPool2d
            output = SparseAvgPooling(
                m.kernel_size,
                m.stride,
                m.padding,
                ceil_mode=m.ceil_mode,
                count_include_pad=m.count_include_pad,
                divisor_override=m.divisor_override)

        elif isinstance(m, (nn.BatchNorm2d, nn.SyncBatchNorm)):
            m: nn.BatchNorm2d
            output = (SparseSyncBatchNorm2d
                      if enable_sync_bn else SparseBatchNorm2d)(
                          m.weight.shape[0],
                          eps=m.eps,
                          momentum=m.momentum,
                          affine=m.affine,
                          track_running_stats=m.track_running_stats)
            output.weight.data.copy_(m.weight.data)
            output.bias.data.copy_(m.bias.data)
            output.running_mean.data.copy_(m.running_mean.data)
            output.running_var.data.copy_(m.running_var.data)
            output.num_batches_tracked.data.copy_(m.num_batches_tracked.data)

        elif isinstance(m, (nn.Conv1d, )):
            raise NotImplementedError

        for name, child in m.named_children():
            output.add_module(
                name,
                self.dense_model_to_sparse(
                    child, enable_sync_bn=enable_sync_bn))
        del m
        return output
