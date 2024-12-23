import time, torch
from argparse import ArgumentTypeError
from prefetch_generator import BackgroundGenerator
import logging
from sklearn.metrics import accuracy_score, average_precision_score, precision_score, f1_score, recall_score
import numpy as np

def compute_confusion_matrix_elements(y_true, y_pred):
    """
    Compute True Positives (TP), True Negatives (TN), False Positives (FP)
    for a multi-class problem given two lists of true and predicted labels.
    
    :param y_true: List of true class labels
    :param y_pred: List of predicted class labels
    :return: Dictionary with classes as keys and a sub-dictionary containing
             TP, TN, FP for each class.
    """
    classes = np.unique(np.concatenate((y_true, y_pred)))
    metrics = {cls: {'TP': 0, 'TN': 0, 'FP': 0} for cls in classes}
    
    for true_label, pred_label in zip(y_true, y_pred):
        for cls in classes:
            if true_label == cls:
                if pred_label == cls:
                    metrics[cls]['TP'] += 1
                # No need to increment FN explicitly as it's implied by absence of TP or FP
            else:
                if pred_label == cls:
                    metrics[cls]['FP'] += 1
                else:
                    metrics[cls]['TN'] += 1
                    
    return metrics

class WeightedSubset(torch.utils.data.Subset):
    def __init__(self, dataset, indices, weights) -> None:
        self.dataset = dataset
        assert len(indices) == len(weights)
        self.indices = indices
        self.weights = weights

    def __getitem__(self, idx):
        if isinstance(idx, list):
            return self.dataset[[self.indices[i] for i in idx]], self.weights[[i for i in idx]]
        return self.dataset[self.indices[idx]], self.weights[idx]

def evaluate_accuracy(data_iter, net, device):

    acc_sum, n = 0.0, 0
    true_labels = []
    model_preds = []
    with torch.no_grad():
        net.eval()
        for X, y in data_iter:
            logits = net(X.to(device))[0]
#             if isinstance(logits,tuple):
#                 logits = logits[0]

            model_pred = logits.argmax(dim=1)
            true_label = y.to(device)

            acc_sum += (model_pred == true_label).float().sum().cpu().item()

            true_label = [int(item.float().cpu().item()) for item in true_label]
            model_pred = [int(item.float().cpu().item()) for item in model_pred]
            
            true_labels+=true_label
            model_preds+= model_pred
            n += y.shape[0]
        net.train()  # 改回训练模式
    return acc_sum / n,true_labels,model_preds

def train(train_loader, network, criterion, criterion1, model_teacher, optimizer, scheduler, epoch, args, rec, if_weighted: bool = False):
    """Train for one epoch on the training set"""
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')

    # switch to train mode
    network.train()
    if model_teacher is not None:
        model_teacher.eval()

    end = time.time()
    for i, contents in enumerate(train_loader):
        optimizer.zero_grad()
        if if_weighted:
            target = contents[0][1].to(args.device)
            input = contents[0][0].to(args.device)

            # Compute output
            output = network(input)
            weights = contents[1].to(args.device).requires_grad_(False)
            loss = torch.sum(criterion(output, target) * weights) / torch.sum(weights)
        else:
            target = contents[1].to(args.device)
            input = contents[0].to(args.device)

            # Compute output
            output,output2 = network(input)
            if model_teacher is not None:
                output_teacher = model_teacher(input)
                loss = criterion(output, output_teacher)
                losses.update(loss.item(), input.size(0))
            else:
                loss = criterion(output, target).mean()+criterion1(output2,target)*0
                losses.update(loss.data.item(), input.size(0))

        # Measure accuracy and record loss
        prec1 = accuracy(output.data, target, topk=(1,))[0]
        top1.update(prec1.item(), input.size(0))

        # Compute gradient and do SGD step
        loss.backward()
        optimizer.step()

        # Measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            logging.info('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'LR {lr:.5f}'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                loss=losses, top1=top1, lr=_get_learning_rate(optimizer)))
            
    scheduler.step()
    record_train_stats(rec, epoch, losses.avg, top1.avg, optimizer.state_dict()['param_groups'][0]['lr'])


def test(test_loader, network, criterion, epoch, args, rec):
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')

    # Switch to evaluate mode
    network.eval()
    network.no_grad = True

    end = time.time()
    for i, (input, target) in enumerate(test_loader):
        target = target.to(args.device)
        input = input.to(args.device)

        # Compute output
        with torch.no_grad():
            output = network(input)[0]
            # print(output.shape)

            loss = criterion(output, target).mean()

        # Measure accuracy and record loss
        prec1 = accuracy(output.data, target, topk=(1,))[0]
        losses.update(loss.data.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        # Measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            logging.info('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                i, len(test_loader), batch_time=batch_time, loss=losses,
                top1=top1))

    logging.info(' * Prec@1 {top1.avg:.3f}'.format(top1=top1))
    test_acc,true_labels,model_preds = evaluate_accuracy(test_loader, network,args.device)
    acc = round(accuracy_score(true_labels,model_preds),4)
    pre = round(precision_score(true_labels,model_preds,average='macro'),4)
    recall = round(recall_score(true_labels,model_preds,average='macro'),4)
    f1 = round(f1_score(true_labels,model_preds,average='macro'),4)
    spe = compute_confusion_matrix_elements(true_labels,model_preds)
    print(acc,pre,recall,f1)
    print(spe)

    network.no_grad = False

    record_test_stats(rec, epoch, losses.avg, top1.avg)
    return top1.avg


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, name, fmt=':f'):
        self.name = name
        self.fmt = fmt
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

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


def str_to_bool(v):
    # Handle boolean type in arguments.
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise ArgumentTypeError('Boolean value expected.')

def _get_learning_rate(optimizer):
    return max(param_group['lr'] for param_group in optimizer.param_groups)

def save_checkpoint(state, path, epoch, prec):
    logging.info("=> Saving checkpoint for epoch %d, with Prec@1 %f." % (epoch, prec))
    torch.save(state, path)


def init_recorder():
    from types import SimpleNamespace
    rec = SimpleNamespace()
    rec.train_step = []
    rec.train_loss = []
    rec.train_acc = []
    rec.lr = []
    rec.test_step = []
    rec.test_loss = []
    rec.test_acc = []
    rec.ckpts = []
    return rec


def record_train_stats(rec, step, loss, acc, lr):
    rec.train_step.append(step)
    rec.train_loss.append(loss)
    rec.train_acc.append(acc)
    rec.lr.append(lr)
    return rec


def record_test_stats(rec, step, loss, acc):
    rec.test_step.append(step)
    rec.test_loss.append(loss)
    rec.test_acc.append(acc)
    return rec


def record_ckpt(rec, step):
    rec.ckpts.append(step)
    return rec


class DataLoaderX(torch.utils.data.DataLoader):
    def __iter__(self):
        return BackgroundGenerator(super().__iter__())
