"""
train_isd.py

Reference:
[1] Ajinkya Tejankar1,Soroush Abbasi Koohpayegani, Vipin Pillai, Paolo Favaro, Hamed Pirsiavash
    ISD: Self-Supervised Learning by Iterative Similarity Distillation
"""

import builtins
import os
import sys
import time
import argparse
import socket
import random

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torchvision import transforms, datasets

from PIL import ImageFilter
import numpy as np

from utils import adjust_learning_rate, AverageMeter
import resnet
from tools import get_logger


def parse_option():

    parser = argparse.ArgumentParser('argument for training')

    parser.add_argument('--data_path', type=str, default='./data', help='path to dataset')
    parser.add_argument('--debug', action='store_true', help='whether in debug mode or not')

    parser.add_argument('--print_freq', type=int, default=100, help='print frequency')
    parser.add_argument('--save_freq', type=int, default=2, help='save frequency')
    parser.add_argument('--batch_size', type=int, default=256, help='batch_size')
    parser.add_argument('--num_workers', type=int, default=2, help='num of workers to use')
    parser.add_argument('--epochs', type=int, default=150, help='number of training epochs')

    # optimization
    parser.add_argument('--learning_rate', type=float, default=0.01, help='learning rate')
    parser.add_argument('--lr_decay_epochs', type=str, default='90,120', help='where to decay lr, can be a list')
    parser.add_argument('--lr_decay_rate', type=float, default=0.2, help='decay rate for learning rate')
    parser.add_argument('--cos', action='store_true', help='whether to cosine learning rate or not')
    parser.add_argument('--weight_decay', type=float, default=1e-4, help='weight decay')
    parser.add_argument('--sgd_momentum', type=float, default=0.9, help='SGD momentum')


    # model settings
    parser.add_argument('--arch', type=str, default='MyResNet',
                        choices=['MyResNet' , 'ResNet18'])

    # isd settings
    parser.add_argument('--queue_size', type=int, default=128000)
    parser.add_argument('--temp', type=float, default=0.02)
    parser.add_argument('--momentum', type=float, default=0.999)
    parser.add_argument('--augmentation', type=str, default='weak/strong',
                        choices=['weak/strong', 'weak/weak', 'strong/weak', 'strong/strong'],
                        help='use full or subset of the dataset')

    parser.add_argument('--checkpoint_path', default='output/', type=str,
                        help='where to save checkpoints.')

    parser.add_argument('--resume_path', default='', type=str,
                        help='path to latest checkpoint (default: none)')

    opt = parser.parse_args()

    iterations = opt.lr_decay_epochs.split(',')
    opt.lr_decay_epochs = list([])
    for it in iterations:
        opt.lr_decay_epochs.append(int(it))

    return opt

class KLD(nn.Module):
    def forward(self, inputs, targets):
        inputs = F.log_softmax(inputs, dim=1)
        targets = F.softmax(targets, dim=1)
        return F.kl_div(inputs, targets, reduction='batchmean')


class ISD(nn.Module):
    def __init__(self, arch, K=65536, m=0.999, T=0.02):
        super(ISD, self).__init__()

        self.K = K
        self.m = m
        self.T = T

        # create encoders and projection layers
        if 'ResNet' in arch:
            # both encoders should have same arch
            self.encoder_q = resnet.__dict__[arch]()
            self.encoder_k = resnet.__dict__[arch]()

            # save output embedding dimensions
            # assuming that both encoders have same dim
            feat_dim = self.encoder_q.linear.in_features
            out_dim = feat_dim

            ##### prediction layer ####
            # remove linear layers for encoders
            self.encoder_k.linear = nn.Sequential()
            self.encoder_q.linear = nn.Sequential()

            # add a prediction layer for q with BN
            self.predict_q = nn.Sequential(
                nn.Linear(feat_dim, feat_dim, bias=False),
                nn.BatchNorm1d(feat_dim),
                nn.ReLU(inplace=True),
                nn.Linear(feat_dim, feat_dim, bias=True),
            )     
        else:
            raise ValueError('arch not found: {}'.format(arch))

        # copy query encoder weights to key encoder
        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data.copy_(param_q.data)
            param_k.requires_grad = False

        # setup queue
        self.register_buffer('queue', torch.randn(self.K, out_dim))
        # normalize the queue
        self.queue = nn.functional.normalize(self.queue, dim=0)

        # setup the queue pointer
        self.register_buffer('queue_ptr', torch.zeros(1, dtype=torch.long))


    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)


    @torch.no_grad()
    def data_parallel(self):
        self.encoder_q = torch.nn.DataParallel(self.encoder_q)
        self.encoder_k = torch.nn.DataParallel(self.encoder_k)
        self.predict_q = torch.nn.DataParallel(self.predict_q)


    @torch.no_grad()
    def _dequeue_and_enqueue(self, keys):
        batch_size = keys.shape[0]

        ptr = int(self.queue_ptr)
        assert self.K % batch_size == 0

        # replace the keys at ptr (dequeue and enqueue)
        self.queue[ptr:ptr + batch_size] = keys
        ptr = (ptr + batch_size) % self.K  # move pointer

        self.queue_ptr[0] = ptr


    def forward(self, im_q, im_k):
        # compute query features
        feat_q = self.encoder_q(im_q)
        # compute prediction queries
        q = self.predict_q(feat_q)
        q = nn.functional.normalize(q, dim=1)

        # compute key features
        with torch.no_grad():
            # update the key encoder
            self._momentum_update_key_encoder()

            # shuffle keys
            shuffle_ids, reverse_ids = get_shuffle_ids(im_k.shape[0])
            im_k = im_k[shuffle_ids]

            # forward through the key encoder
            k = self.encoder_k(im_k)
            k = nn.functional.normalize(k, dim=1)

            # undo shuffle
            k = k[reverse_ids]

        # calculate similarities
        queue = self.queue.clone().detach()
        sim_q = torch.mm(q, queue.t())
        sim_k = torch.mm(k, queue.t())

        # scale the similarities with temperature
        sim_q /= self.T
        sim_k /= self.T

        # dequeue and enqueue
        self._dequeue_and_enqueue(k)

        return sim_q, sim_k


def get_shuffle_ids(bsz):
    """generate shuffle ids for ShuffleBN"""
    forward_inds = torch.randperm(bsz).long().cuda()
    backward_inds = torch.zeros(bsz).long().cuda()
    value = torch.arange(bsz).long().cuda()
    backward_inds.index_copy_(0, forward_inds, value)
    return forward_inds, backward_inds


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


class GaussianBlur(object):
    """Gaussian blur augmentation in SimCLR https://arxiv.org/abs/2002.05709"""
    def __init__(self, sigma=[.1, 2.]):
        self.sigma = sigma

    def __call__(self, x):
        sigma = random.uniform(self.sigma[0], self.sigma[1])
        x = x.filter(ImageFilter.GaussianBlur(radius=sigma))
        return x


# Create train loader
def get_train_loader(opt):
    image_size = 32
    mean = [0.4914, 0.4822, 0.4465]
    std = [0.2023, 0.1994, 0.2010]
    normalize = transforms.Normalize(mean=mean, std=std)

    aug_strong = transforms.Compose([
        transforms.RandomResizedCrop(image_size, scale=(0.2, 1.)),
        transforms.RandomApply([
            transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)  # not strengthened
        ], p=0.8),
        transforms.RandomGrayscale(p=0.2),
        transforms.RandomApply([GaussianBlur([.1, 2.])], p=0.5),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize
    ])

    aug_weak = transforms.Compose([
        transforms.RandomResizedCrop(image_size, scale=(0.2, 1.)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize
    ])

    if opt.augmentation == 'weak/strong':
        train_dataset = datasets.CIFAR10(
            root=opt.data_path,
            train=True,
            download=True,
            transform=TwoCropsTransform(k_t=aug_weak, q_t=aug_strong)
        )
    elif opt.augmentation == 'weak/weak':
        train_dataset = datasets.CIFAR10(
            root=opt.data_path,
            train=True,
            download=True,
            transform=TwoCropsTransform(k_t=aug_weak, q_t=aug_weak)
        )
    elif opt.augmentation == 'strong/weak':
        train_dataset = datasets.CIFAR10(
            root=opt.data_path,
            train=True,
            download=True,
            transform=TwoCropsTransform(k_t=aug_strong, q_t=aug_weak)
        )
    else: # strong/strong
        train_dataset = datasets.CIFAR10(
            root=opt.data_path,
            train=True,
            download=True,
            transform=TwoCropsTransform(k_t=aug_strong, q_t=aug_strong)
        )

    print('==> train dataset')
    print(train_dataset)

    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=opt.batch_size,
        shuffle=True,
        num_workers=opt.num_workers,
        pin_memory=True,
        drop_last=True)

    return train_loader


def main():
    args = parse_option()
    os.makedirs(args.checkpoint_path, exist_ok=True)

    if not args.debug:
        os.environ['PYTHONBREAKPOINT'] = '0'
        logger = get_logger(
            logpath=os.path.join(args.checkpoint_path, 'logs'),
            filepath=os.path.abspath(__file__)
        )
        def print_pass(*args):
            logger.info(*args)
        builtins.print = print_pass

    print(args)

    train_loader = get_train_loader(args)

    isd = ISD(args.arch, K=args.queue_size, m=args.momentum, T=args.temp)
    isd.data_parallel()
    isd = isd.cuda()

    print(isd)

    criterion = KLD().cuda()

    params = [p for p in isd.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params,
                                lr=args.learning_rate,
                                momentum=args.sgd_momentum,
                                weight_decay=args.weight_decay)

    cudnn.benchmark = True
    args.start_epoch = 1

    if args.resume_path:
        print('==> resume from checkpoint: {}'.format(args.resume_path))
        ckpt = torch.load(args.resume_path)
        print('==> resume from epoch: {}'.format(ckpt['epoch']))
        isd.load_state_dict(ckpt['state_dict'], strict=True)
        optimizer.load_state_dict(ckpt['optimizer'])
        args.start_epoch = ckpt['epoch'] + 1


    # routine
    for epoch in range(args.start_epoch, args.epochs + 1):

        adjust_learning_rate(epoch, args, optimizer)
        print("==> training...")

        time1 = time.time()
        loss = train_student(epoch, train_loader, isd, criterion, optimizer, args)

        time2 = time.time()
        print('epoch {}, total time {:.2f}'.format(epoch, time2 - time1))

        # saving the model
        if epoch % args.save_freq == 0:
            print('==> Saving...')
            state = {
                'opt': args,
                'state_dict': isd.state_dict(),
                'optimizer': optimizer.state_dict(),
                'epoch': epoch,
            }

            save_file = os.path.join(args.checkpoint_path, 'ckpt_epoch_{epoch}.pth'.format(epoch=epoch))
            torch.save(state, save_file)

            # help release GPU memory
            del state
            torch.cuda.empty_cache()


def train_student(epoch, train_loader, isd, criterion, optimizer, opt):
    """
    one epoch training for CompReSS
    """
    isd.train()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    loss_meter = AverageMeter()

    end = time.time()
    for idx, ((im_q, im_k), _) in enumerate(train_loader):
        data_time.update(time.time() - end)
        im_q = im_q.cuda(non_blocking=True)
        im_k = im_k.cuda(non_blocking=True)

        # ===================forward=====================
        sim_q, sim_k = isd(im_q=im_q, im_k=im_k)
        loss = criterion(inputs=sim_q, targets=sim_k)

        # ===================backward=====================
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # ===================meters=====================
        loss_meter.update(loss.item(), im_q.size(0))

        torch.cuda.synchronize()
        batch_time.update(time.time() - end)
        end = time.time()

        # print info
        if (idx + 1) % opt.print_freq == 0:
            print('Train: [{0}][{1}/{2}]\t'
                  'BT {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'DT {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'loss {loss.val:.3f} ({loss.avg:.3f})\t'.format(
                   epoch, idx + 1, len(train_loader), batch_time=batch_time,
                   data_time=data_time, loss=loss_meter))
            sys.stdout.flush()

    return loss_meter.avg


if __name__ == '__main__':
    main()
