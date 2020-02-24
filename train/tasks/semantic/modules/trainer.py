#!/usr/bin/env python3
# This file is covered by the LICENSE file in the root of this project.
import datetime
import imp
import os
import time
import torch.nn.functional as F
import __init__ as booger
import cv2
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.optim as optim
from common.avgmeter import *
from common.logger import Logger
from common.sync_batchnorm.batchnorm import convert_model
from common.warmupLR import *
from matplotlib import pyplot as plt
from tasks.semantic.modules.ioueval import *
from tasks.semantic.modules.segmentator import *

import torch
import torch.nn as nn
import torch.nn.functional as F




def save_to_log(logdir,logfile,message):
    f = open(logdir+'/'+logfile, "a")
    f.write(message+'\n')
    f.close()
    return


def save_checkpoint(model, logdir, suffix=""):
    # Save the weights
    torch.save(model.state_dict(), logdir +
               "/SalsaNet" + suffix)






class Trainer():
    def __init__(self, ARCH, DATA, datadir, logdir, path=None,model_mode='salsanext'):
        # parameters
        self.ARCH = ARCH
        self.DATA = DATA
        self.datadir = datadir
        self.log = logdir
        self.path = path
        self.model_mode = model_mode

        self.batch_time_t = AverageMeter()
        self.data_time_t = AverageMeter()
        self.batch_time_e = AverageMeter()


        # put logger where it belongs

        self.info = {"train_update": 0,
                     "train_loss": 0,
                     "train_acc": 0,
                     "train_iou": 0,
                     "valid_loss": 0,
                     "valid_acc": 0,
                     "valid_iou": 0,
                     "backbone_lr": 0,
                     "decoder_lr": 0,
                     "head_lr": 0,
                     "post_lr": 0}

        # get the data
        parserModule = imp.load_source("parserModule",
                                       booger.TRAIN_PATH + '/tasks/semantic/dataset/' +
                                       self.DATA["name"] + '/parser.py')
        self.parser = parserModule.Parser(root=self.datadir,
                                          train_sequences=self.DATA["split"]["train"],
                                          valid_sequences=self.DATA["split"]["valid"],
                                          test_sequences=None,
                                          labels=self.DATA["labels"],
                                          color_map=self.DATA["color_map"],
                                          learning_map=self.DATA["learning_map"],
                                          learning_map_inv=self.DATA["learning_map_inv"],
                                          sensor=self.ARCH["dataset"]["sensor"],
                                          max_points=self.ARCH["dataset"]["max_points"],
                                          batch_size=self.ARCH["train"]["batch_size"],
                                          workers=self.ARCH["train"]["workers"],
                                          gt=True,
                                          shuffle_train=True)

        # weights for loss (and bias)
        # weights for loss (and bias)
        epsilon_w = self.ARCH["train"]["epsilon_w"]
        content = torch.zeros(self.parser.get_n_classes(), dtype=torch.float)
        for cl, freq in DATA["content"].items():
            x_cl = self.parser.to_xentropy(cl)  # map actual class to xentropy class
            content[x_cl] += freq
        self.loss_w = 1 / (content + epsilon_w)  # get weights
        for x_cl, w in enumerate(self.loss_w):  # ignore the ones necessary to ignore
            if DATA["learning_ignore"][x_cl]:
                # don't weigh
                self.loss_w[x_cl] = 0
        print("Loss weights from content: ", self.loss_w.data)
        # self.focal_loss = FocalLoss2d(reduce='mean', weight=self.loss_w.type(torch.float32).cuda()).cuda()
        # concatenate the encoder and the head
        if self.model_mode in ('salsanet','salsanext'):
            with torch.no_grad():
                self.model = SalsaNet(self.ARCH,
                                      self.parser.get_n_classes(),
                                      self.path)
        else:
            with torch.no_grad():
                self.model = Segmentator(self.ARCH,
                                      self.parser.get_n_classes(),
                                      self.path)

        self.tb_logger = Logger(self.log + "/tb", self.model)

        # GPU?
        self.gpu = False
        self.multi_gpu = False
        self.n_gpus = 0
        self.model_single = self.model
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print("Training in device: ", self.device)
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            cudnn.benchmark = True
            cudnn.fastest = True
            self.gpu = True
            self.n_gpus = 1
            self.model.cuda()
        if torch.cuda.is_available() and torch.cuda.device_count() > 1:
            print("Let's use", torch.cuda.device_count(), "GPUs!")
            self.model = nn.DataParallel(self.model)  # spread in gpus
            self.model = convert_model(self.model).cuda()  # sync batchnorm
            self.model_single = self.model.module  # single model to get weight names
            self.multi_gpu = True
            self.n_gpus = torch.cuda.device_count()

        if self.model_mode in ('salsanet','salsanext'):
            if self.path is not None:
                w_dict = torch.load(path + "/SalsaNet",
                                    map_location=lambda storage, loc: storage)
                self.model.load_state_dict(w_dict, strict=True)

        # loss
        if "loss" in self.ARCH["train"].keys() and self.ARCH["train"]["loss"] == "xentropy":
            self.criterion = nn.NLLLoss(weight=self.loss_w).to(self.device)
        else:
            raise Exception('Loss not defined in config file')
        # loss as dataparallel too (more images in batch)
        if self.n_gpus > 1:
            self.criterion = nn.DataParallel(self.criterion).cuda()  # spread in gpus

        if self.model_mode not in ('salsanet','salsanext'):
            if self.ARCH["post"]["CRF"]["use"] and self.ARCH["post"]["CRF"]["train"]:
                self.lr_group_names = ["post_lr"]
                self.train_dicts = [{'params': self.model_single.CRF.parameters()}]
            else:
                self.lr_group_names = []
                self.train_dicts = []
            if self.ARCH["backbone"]["train"]:
                self.lr_group_names.append("backbone_lr")
                self.train_dicts.append(
                    {'params': self.model_single.backbone.parameters()})
            if self.ARCH["decoder"]["train"]:
                self.lr_group_names.append("decoder_lr")
                self.train_dicts.append(
                    {'params': self.model_single.decoder.parameters()})
            if self.ARCH["head"]["train"]:
                self.lr_group_names.append("head_lr")
                self.train_dicts.append({'params': self.model_single.head.parameters()})

            # Use SGD optimizer to train
            self.optimizer = optim.SGD(self.train_dicts,
                                       lr=self.ARCH["train"]["lr"],
                                       momentum=self.ARCH["train"]["momentum"],
                                       weight_decay=self.ARCH["train"]["w_decay"])
        else:
            self.optimizer = optim.SGD([{'params':self.model.parameters()}],
                                       lr=self.ARCH["train"]["lr"],
                                       momentum=self.ARCH["train"]["momentum"],
                                       weight_decay=self.ARCH["train"]["w_decay"])

        # Use warmup learning rate
        # post decay and step sizes come in epochs and we want it in steps
        steps_per_epoch = self.parser.get_train_size()
        up_steps = int(self.ARCH["train"]["wup_epochs"] * steps_per_epoch)
        final_decay = self.ARCH["train"]["lr_decay"] ** (1 / steps_per_epoch)
        self.scheduler = warmupLR(optimizer=self.optimizer,
                                  lr=self.ARCH["train"]["lr"],
                                  warmup_steps=up_steps,
                                  momentum=self.ARCH["train"]["momentum"],
                                  decay=final_decay)

    def calculate_estimate(self,epoch, iter):
        estimate = int((self.data_time_t.avg + self.batch_time_t.avg) *\
                       (self.parser.get_train_size() * self.ARCH['train']['max_epochs'] - (iter + 1 + epoch * self.parser.get_train_size()))) +\
            int(self.batch_time_e.avg * self.parser.get_valid_size() * (self.ARCH['train']['max_epochs'] - (epoch+1)))
        return str(datetime.timedelta(seconds=estimate))

    @staticmethod
    def get_mpl_colormap(cmap_name):
        cmap = plt.get_cmap(cmap_name)
        # Initialize the matplotlib color map
        sm = plt.cm.ScalarMappable(cmap=cmap)
        # Obtain linear color range
        color_range = sm.to_rgba(np.linspace(0, 1, 256), bytes=True)[:, 2::-1]
        return color_range.reshape(256, 1, 3)

    @staticmethod
    def make_log_img(depth, mask, pred, gt, color_fn):
        # input should be [depth, pred, gt]
        # make range image (normalized to 0,1 for saving)
        depth = (cv2.normalize(depth, None, alpha=0, beta=1,
                               norm_type=cv2.NORM_MINMAX,
                               dtype=cv2.CV_32F) * 255.0).astype(np.uint8)
        out_img = cv2.applyColorMap(
            depth, Trainer.get_mpl_colormap('viridis')) * mask[..., None]
        # make label prediction
        pred_color = color_fn((pred * mask).astype(np.int32))
        out_img = np.concatenate([out_img, pred_color], axis=0)
        # make label gt
        gt_color = color_fn(gt)
        out_img = np.concatenate([out_img, gt_color], axis=0)
        return (out_img).astype(np.uint8)

    @staticmethod
    def save_to_log(logdir, logger, info, epoch, w_summary=False, model=None, img_summary=False, imgs=[]):
        # save scalars
        for tag, value in info.items():
            logger.scalar_summary(tag, value, epoch)

        # save summaries of weights and biases
        if w_summary and model:
            for tag, value in model.named_parameters():
                tag = tag.replace('.', '/')
                logger.histo_summary(tag, value.data.cpu().numpy(), epoch)
                if value.grad is not None:
                    logger.histo_summary(
                        tag + '/grad', value.grad.data.cpu().numpy(), epoch)

        if img_summary and len(imgs) > 0:
            directory = os.path.join(logdir, "predictions")
            if not os.path.isdir(directory):
                os.makedirs(directory)
            for i, img in enumerate(imgs):
                name = os.path.join(directory, str(i) + ".png")
                cv2.imwrite(name, img)

    def train(self):
        # accuracy and IoU stuff
        best_train_iou = 0.0
        best_val_iou = 0.0

        self.ignore_class = []
        for i, w in enumerate(self.loss_w):
            if w < 1e-10:
                self.ignore_class.append(i)
                print("Ignoring class ", i, " in IoU evaluation")
        self.evaluator = iouEval(self.parser.get_n_classes(),
                                 self.device, self.ignore_class)

        # train for n epochs
        for epoch in range(self.ARCH["train"]["max_epochs"]):
            # get info for learn rate currently
            # groups = self.optimizer.param_groups
            # for name, g in zip(self.lr_group_names, groups):
            #     self.info[name] = g['lr']
            # train for 1 epoch
            acc, iou, loss, update_mean = self.train_epoch(train_loader=self.parser.get_train_set(),
                                                           model=self.model,
                                                           criterion=self.criterion,
                                                           optimizer=self.optimizer,
                                                           epoch=epoch,
                                                           evaluator=self.evaluator,
                                                           scheduler=self.scheduler,
                                                           color_fn=self.parser.to_color,
                                                           report=self.ARCH["train"]["report_batch"],
                                                           show_scans=self.ARCH["train"]["show_scans"])

            # update info
            self.info["train_update"] = update_mean
            self.info["train_loss"] = loss
            self.info["train_acc"] = acc
            self.info["train_iou"] = iou

            # remember best iou and save checkpoint
            if iou > best_train_iou:
                print("Best mean iou in training set so far, save model!")
                best_train_iou = iou
                if self.model_mode in ('salsanet','salsanext'):
                    save_checkpoint(self.model, self.log, suffix="_train")
                else:
                    self.model_single.save_checkpoint(self.log, suffix="_train")

            if epoch % self.ARCH["train"]["report_epoch"] == 0:
                # evaluate on validation set
                print("*" * 80)
                acc, iou, loss, rand_img = self.validate(val_loader=self.parser.get_valid_set(),
                                                         model=self.model,
                                                         criterion=self.criterion,
                                                         evaluator=self.evaluator,
                                                         class_func=self.parser.get_xentropy_class_string,
                                                         color_fn=self.parser.to_color,
                                                         save_scans=self.ARCH["train"]["save_scans"])

                # update info
                self.info["valid_loss"] = loss
                self.info["valid_acc"] = acc
                self.info["valid_iou"] = iou

                # remember best iou and save checkpoint
                if iou > best_val_iou:
                    print("Best mean iou in validation so far, save model!")
                    print("*" * 80)
                    best_val_iou = iou

                    # save the weights!
                    if self.model_mode in ('salsanet', 'salsanext'):
                        save_checkpoint(self.model, self.log, suffix="")
                    else:
                        self.model_single.save_checkpoint(self.log, suffix="")

                print("*" * 80)

                # save to log
                Trainer.save_to_log(logdir=self.log,
                                    logger=self.tb_logger,
                                    info=self.info,
                                    epoch=epoch,
                                    w_summary=self.ARCH["train"]["save_summary"],
                                    model=self.model_single,
                                    img_summary=self.ARCH["train"]["save_scans"],
                                    imgs=rand_img)

        print('Finished Training')

        return

    def train_epoch(self, train_loader, model, criterion, optimizer, epoch, evaluator, scheduler, color_fn, report=10,
                    show_scans=False):
        losses = AverageMeter()
        acc = AverageMeter()
        iou = AverageMeter()
        update_ratio_meter = AverageMeter()

        # empty the cache to train now
        if self.gpu:
            torch.cuda.empty_cache()

        # switch to train mode
        model.train()

        end = time.time()
        for i, (in_vol, proj_mask, proj_labels, _, path_seq, path_name, _, _, _, _, _, _, _, _, _) in enumerate(train_loader):
        #for i, (in_vol, proj_mask, proj_labels) in enumerate(train_loader):
            # measure data loading time
            self.data_time_t.update(time.time() - end)
            if not self.multi_gpu and self.gpu:
                in_vol = in_vol.cuda()
                proj_mask = proj_mask.cuda()
            if self.gpu:
                proj_labels = proj_labels.cuda(non_blocking=True).long()

            # compute output
            output = model(in_vol)
            loss = criterion(torch.log(output.clamp(min=1e-8)), proj_labels)

            optimizer.zero_grad()
            if self.n_gpus > 1:
                try:
                    idx = torch.ones(self.n_gpus).cuda()
                    loss.backward(idx)
                except:
                    print(output.shape)
                    print(proj_labels.shape)
            else:
                loss.backward()
            optimizer.step()

            # measure accuracy and record loss
            loss = loss.mean()
            with torch.no_grad():
                evaluator.reset()
                argmax = output.argmax(dim=1)
                evaluator.addBatch(argmax, proj_labels)
                accuracy = evaluator.getacc()
                jaccard, class_jaccard = evaluator.getIoU()
            losses.update(loss.item(), in_vol.size(0))
            acc.update(accuracy.item(), in_vol.size(0))
            iou.update(jaccard.item(), in_vol.size(0))

            # measure elapsed time
            self.batch_time_t.update(time.time() - end)
            end = time.time()

            # get gradient updates and weights, so I can print the relationship of
            # their norms
            update_ratios = []
            for g in self.optimizer.param_groups:
                lr = g["lr"]
                for value in g["params"]:
                    if value.grad is not None:
                        w = np.linalg.norm(value.data.cpu().numpy().reshape((-1)))
                        update = np.linalg.norm(-max(lr, 1e-10) *
                                                value.grad.cpu().numpy().reshape((-1)))
                        update_ratios.append(update / max(w, 1e-10))
            update_ratios = np.array(update_ratios)
            update_mean = update_ratios.mean()
            update_std = update_ratios.std()
            update_ratio_meter.update(update_mean)  # over the epoch

            if show_scans:
                # get the first scan in batch and project points
                mask_np = proj_mask[0].cpu().numpy()
                depth_np = in_vol[0][0].cpu().numpy()
                pred_np = argmax[0].cpu().numpy()
                gt_np = proj_labels[0].cpu().numpy()
                out = Trainer.make_log_img(depth_np, mask_np, pred_np, gt_np, color_fn)
                cv2.imshow("sample_training", out)
                cv2.waitKey(1)

            if i % self.ARCH["train"]["report_batch"] == 0:
                print('Lr: {lr:.3e} | '
                      'Update: {umean:.3e} mean,{ustd:.3e} std | '
                      'Epoch: [{0}][{1}/{2}] | '
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f}) | '
                      'Data {data_time.val:.3f} ({data_time.avg:.3f}) | '
                      'Loss {loss.val:.4f} ({loss.avg:.4f}) | '
                      'acc {acc.val:.3f} ({acc.avg:.3f}) | '
                      'IoU {iou.val:.3f} ({iou.avg:.3f}) | [{estim}]'.format(
                    epoch, i, len(train_loader), batch_time=self.batch_time_t,
                    data_time=self.data_time_t, loss=losses, acc=acc, iou=iou, lr=lr,
                    umean=update_mean, ustd=update_std,estim=self.calculate_estimate(epoch,i)))

                save_to_log(self.log,'log.txt','Lr: {lr:.3e} | '
                      'Update: {umean:.3e} mean,{ustd:.3e} std | '
                      'Epoch: [{0}][{1}/{2}] | '
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f}) | '
                      'Data {data_time.val:.3f} ({data_time.avg:.3f}) | '
                      'Loss {loss.val:.4f} ({loss.avg:.4f}) | '
                      'acc {acc.val:.3f} ({acc.avg:.3f}) | '
                      'IoU {iou.val:.3f} ({iou.avg:.3f}) | [{estim}]'.format(
                    epoch, i, len(train_loader), batch_time=self.batch_time_t,
                    data_time=self.data_time_t, loss=losses, acc=acc, iou=iou, lr=lr,
                    umean=update_mean, ustd=update_std,estim=self.calculate_estimate(epoch,i)))

            # step scheduler
            scheduler.step()

        return acc.avg, iou.avg, losses.avg, update_ratio_meter.avg

    def validate(self, val_loader, model, criterion, evaluator, class_func, color_fn, save_scans):
        losses = AverageMeter()
        acc = AverageMeter()
        iou = AverageMeter()
        rand_imgs = []

        # switch to evaluate mode
        model.eval()
        evaluator.reset()

        # empty the cache to infer in high res
        if self.gpu:
            torch.cuda.empty_cache()

        with torch.no_grad():
            end = time.time()
            for i, (in_vol, proj_mask, proj_labels, _, path_seq, path_name, _, _, _, _, _, _, _, _, _) in enumerate(val_loader):
            #for i, (in_vol, proj_mask, proj_labels) in enumerate(val_loader):
                if not self.multi_gpu and self.gpu:
                    in_vol = in_vol.cuda()
                    proj_mask = proj_mask.cuda()
                if self.gpu:
                    proj_labels = proj_labels.cuda(non_blocking=True).long()

                # compute output
                output = model(in_vol)
                loss = criterion(torch.log(output.clamp(min=1e-8)), proj_labels)

                # measure accuracy and record loss
                argmax = output.argmax(dim=1)
                evaluator.addBatch(argmax, proj_labels)
                losses.update(loss.mean().item(), in_vol.size(0))

                if save_scans:
                    # get the first scan in batch and project points
                    mask_np = proj_mask[0].cpu().numpy()
                    depth_np = in_vol[0][0].cpu().numpy()
                    pred_np = argmax[0].cpu().numpy()
                    gt_np = proj_labels[0].cpu().numpy()
                    out = Trainer.make_log_img(depth_np,
                                               mask_np,
                                               pred_np,
                                               gt_np,
                                               color_fn)
                    rand_imgs.append(out)

                # measure elapsed time
                self.batch_time_e.update(time.time() - end)
                end = time.time()

            accuracy = evaluator.getacc()
            jaccard, class_jaccard = evaluator.getIoU()
            acc.update(accuracy.item(), in_vol.size(0))
            iou.update(jaccard.item(), in_vol.size(0))

            print('Validation set:\n'
                  'Time avg per batch {batch_time.avg:.3f}\n'
                  'Loss avg {loss.avg:.4f}\n'
                  'Acc avg {acc.avg:.3f}\n'
                  'IoU avg {iou.avg:.3f}'.format(batch_time=self.batch_time_e,
                                                 loss=losses,
                                                 acc=acc, iou=iou))

            save_to_log(self.log, 'log.txt', 'Validation set:\n'
                  'Time avg per batch {batch_time.avg:.3f}\n'
                  'Loss avg {loss.avg:.4f}\n'
                  'Acc avg {acc.avg:.3f}\n'
                  'IoU avg {iou.avg:.3f}'.format(batch_time=self.batch_time_e,
                                                 loss=losses,
                                                 acc=acc, iou=iou))
            # print also classwise
            for i, jacc in enumerate(class_jaccard):
                print('IoU class {i:} [{class_str:}] = {jacc:.3f}'.format(
                    i=i, class_str=class_func(i), jacc=jacc))
                save_to_log(self.log,'log.txt','IoU class {i:} [{class_str:}] = {jacc:.3f}'.format(
                    i=i, class_str=class_func(i), jacc=jacc))

        return acc.avg, iou.avg, losses.avg, rand_imgs