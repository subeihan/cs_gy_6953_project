"""
utils.py
utility functions for training and testing

Reference:
[1] Ajinkya Tejankar1,Soroush Abbasi Koohpayegani, Vipin Pillai, Paolo Favaro, Hamed Pirsiavash
    ISD: Self-Supervised Learning by Iterative Similarity Distillation
"""

from __future__ import print_function
import math

import torch
import numpy as np

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

# NOTE: assumes that the epoch starts with 1
def adjust_learning_rate(epoch, opt, optimizer):
    if hasattr(opt, 'cos') and opt.cos:
        # NOTE: since epoch starts with 1, we have to subtract 1
        new_lr = opt.learning_rate * 0.5 * (1. + math.cos(math.pi * (epoch-1) / opt.epochs))
        print('LR: {}'.format(new_lr))
        for param_group in optimizer.param_groups:
            param_group['lr'] = new_lr
    else:
        steps = np.sum(epoch > np.asarray(opt.lr_decay_epochs))
        if steps > 0:
            new_lr = opt.learning_rate * (opt.lr_decay_rate ** steps)
            print('LR: {}'.format(new_lr))
            for param_group in optimizer.param_groups:
                param_group['lr'] = new_lr


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(outputs, labels):
    total = 0
    correct = 0
    _, predicted = torch.max(outputs.data, 1)
    total += labels.size(0)
    correct += (predicted == labels).sum().item()
    return correct/total

class TwoCropsTransform:
    def __init__(self, k_t, q_t):
        self.q_t = q_t
        self.k_t = k_t
        print('======= Query transform =======')
        print(self.q_t)
        print('===============================')
        print('======== Key transform ========')
        print(self.k_t)
        print('===============================')

    def __call__(self, x):
        q = self.q_t(x)
        k = self.k_t(x)
        return [q, k]

if __name__ == '__main__':
    meter = AverageMeter()