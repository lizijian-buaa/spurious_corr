import sys
import os
import torch
import torch.nn as nn
import numpy as np
import tqdm


class MaxNorm_via_PGD():
    # learning a max-norm constrainted network via projected gradient descent (PGD)
    def __init__(self, thresh=1.0, LpNorm=2, tau=1):
        self.thresh = thresh
        self.LpNorm = LpNorm
        self.tau = tau
        self.perLayerThresh = []
        
    def setPerLayerThresh(self, model):
        # set per-layer thresholds
        self.perLayerThresh = []
        
        for curLayer in [model.fc.weight, model.fc.bias]: #here we only apply MaxNorm over the last two layers
            curparam = curLayer.data
            if len(curparam.shape)<=1: 
                self.perLayerThresh.append(float('inf'))
                continue
            curparam_vec = curparam.reshape((curparam.shape[0], -1))
            neuronNorm_curparam = torch.linalg.norm(curparam_vec, ord=self.LpNorm, dim=0).detach().unsqueeze(0)
            curLayerThresh = neuronNorm_curparam.min() + self.thresh*(neuronNorm_curparam.max() - neuronNorm_curparam.min())
            self.perLayerThresh.append(curLayerThresh)
                
    def PGD(self, model):
        if len(self.perLayerThresh)==0:
            self.setPerLayerThresh(model)
        
        for i, curLayer in enumerate([model.fc.weight, model.fc.bias]): #here we only apply MaxNorm over the last two layers
            curparam = curLayer.data


            curparam_vec = curparam.reshape((curparam.shape[0], -1))
            neuronNorm_curparam = (torch.linalg.norm(curparam_vec, ord=self.LpNorm, dim=0)**self.tau).detach().unsqueeze(0)
            scalingVect = torch.ones_like(curparam)    
            curLayerThresh = self.perLayerThresh[i]
            
            idx = neuronNorm_curparam > curLayerThresh
            tmp = curLayerThresh / (neuronNorm_curparam[idx].squeeze())**(self.tau)
            idx = idx.squeeze()
            for _ in range(len(scalingVect.shape)-1):
                tmp = tmp.unsqueeze(0)
            scalingVect[:, idx] = torch.mul(scalingVect[:, idx], tmp)
            curparam[:, idx] = scalingVect[:, idx] * curparam[:, idx] 

class Normalizer(): 
    def __init__(self, LpNorm=2, tau=1):
        self.LpNorm = LpNorm
        self.tau = tau
  
    def apply_on(self, model): #this method applies tau-normalization on the classifier layer

        for curLayer in [model.fc.weight]: #change to last layer: Done
            curparam = curLayer.data

            curparam_vec = curparam.reshape((curparam.shape[0], -1))
            neuronNorm_curparam = (torch.linalg.norm(curparam_vec, ord=self.LpNorm, dim=0)**self.tau).detach().unsqueeze(0)
            scalingVect = torch.ones_like(curparam)    
            
            idx = neuronNorm_curparam == neuronNorm_curparam
            tmp = 1 / (neuronNorm_curparam[idx].squeeze())
            idx = idx.squeeze()
            for _ in range(len(scalingVect.shape)-1):
                tmp = tmp.unsqueeze(0)

            scalingVect[:, idx] = torch.mul(scalingVect[:, idx], tmp)
            curparam[:, idx] = scalingVect[:, idx] * curparam[:, idx]


class Logger(object):
    def __init__(self, fpath=None, mode='w'):
        self.console = sys.stdout
        self.file = None
        if fpath is not None:
            self.file = open(fpath, mode)

    def __del__(self):
        self.close()

    def __enter__(self):
        pass

    def __exit__(self, *args):
        self.close()

    def write(self, msg):
        self.console.write(msg)
        if self.file is not None:
            self.file.write(msg)

    def flush(self):
        self.console.flush()
        if self.file is not None:
            self.file.flush()
            os.fsync(self.file.fileno())

    def close(self):
        self.console.close()
        if self.file is not None:
            self.file.close()


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
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


def set_seed(seed):
    """Sets seed"""
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
    torch.manual_seed(seed)
    np.random.seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def get_y_p(g, n_places):
    y = g // n_places
    p = g % n_places
    return y, p


def update_dict(acc_groups, y, g, logits):
    preds = torch.argmax(logits, axis=1)
    correct_batch = (preds == y)
    g = g.cpu()
    for g_val in np.unique(g):
        mask = g == g_val
        n = mask.sum().item()
        corr = correct_batch[mask].sum().item()
        acc_groups[g_val].update(corr / n, n)


def write_dict_to_tb(writer, dict, prefix, step):
    for key, value in dict.items():
        writer.add_scalar(f"{prefix}{key}", value, step)


def get_results(acc_groups, get_yp_func):
    groups = acc_groups.keys()
    results = {
            f"accuracy_{get_yp_func(g)[0]}_{get_yp_func(g)[1]}": acc_groups[g].avg
            for g in groups
    }
    all_correct = sum([acc_groups[g].sum for g in groups])
    all_total = sum([acc_groups[g].count for g in groups])
    results.update({"mean_accuracy" : all_correct / all_total})
    results.update({"worst_accuracy" : min(results.values())})
    return results


def evaluate(model, loader, get_yp_func, multitask=False, predict_place=False):
    model.eval()
    acc_groups = {g_idx : AverageMeter() for g_idx in range(loader.dataset.n_groups)}
    if multitask:
        acc_place_groups = {g_idx: AverageMeter() for g_idx in range(trainset.n_groups)}

    with torch.no_grad():
        for x, y, g, p in tqdm.tqdm(loader):
            x, y, p = x.cuda(), y.cuda(), p.cuda()
            if predict_place:
                y = p

            logits = model(x)
            if multitask:
                logits, logits_place = logits
                update_dict(acc_place_groups, p, g, logits_place)

            update_dict(acc_groups, y, g, logits)
    model.train()
    if multitask:
        return get_results(acc_groups, get_yp_func), get_results(acc_place_groups, get_yp_func)
    return get_results(acc_groups, get_yp_func)


class MultiTaskHead(nn.Module):
    def __init__(self, n_features, n_classes_list):
        super(MultiTaskHead, self).__init__()
        self.fc_list = [
            nn.Linear(n_features, n_classes).cuda()
            for n_classes in n_classes_list
        ]

    def forward(self, x):
        outputs = []
        for head in self.fc_list:
            out = head(x)
            outputs.append(out)
        return outputs