import os
import torchvision
import numpy as np
import matplotlib.pyplot as plt


class PlotTensor:
    """Plot torch tensor as matplotlib figure.

    Args:
        apply_inv (bool): Whether to apply inverse normalization.
    """

    def __init__(self, apply_inv=True) -> None:
        trans = list()
        if apply_inv:
            trans = [torchvision.transforms.Normalize(
                        mean=[ 0., 0., 0. ], std=[1/0.2023, 1/0.1994, 1/0.201]),
                    torchvision.transforms.Normalize(
                        mean=[-0.4914, -0.4822, -0.4465], std=[ 1., 1., 1. ])]
        self.invTrans = torchvision.transforms.Compose(trans)
    
    def plot(self,
             img, nrow=4, title_name=None, save_name=None,
             dpi=None, apply_inv=True):
        assert save_name is not None
        assert img.size(0) % nrow == 0
        ncol = img.size(0) // nrow
        img_grid = torchvision.utils.make_grid(img, nrow=nrow, pad_value=0)
        
        cmap=None
        if img.size(1) == 1:
            cmap = plt.cm.gray
        if apply_inv:
            img_grid = self.invTrans(img_grid)
        img_grid = np.transpose(img_grid.detach().cpu().numpy(), (1, 2, 0))
        fig = plt.figure(figsize=(nrow * 2, ncol * 2))
        plt.imshow(img_grid, cmap=cmap)
        if title_name is not None:
            plt.title(title_name)
        if not os.path.exists(save_name):
            plt.savefig(save_name, dpi=dpi)
        plt.close()
