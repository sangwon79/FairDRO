from __future__ import print_function
import os
import torch
import torch.nn as nn
import time
from utils import get_accuracy
import trainer
from torch.utils.data import DataLoader


class Trainer(trainer.GenericTrainer):
    def __init__(self, args, **kwargs):
        super().__init__(args=args, **kwargs)
        
        self.lamb = args.lamb
        self.fairness_criterion = args.fairness_criterion
        
    def train(self, train_loader, test_loader, epochs, criterion=None, writer=None):
        global loss_set
        model = self.model
        model.train()
        self.n_groups = train_loader.dataset.n_groups
        self.n_classes = train_loader.dataset.n_classes
        if self.fairness_criterion == 'eo':
            self.weights = torch.zeros((self.n_classes, self.n_classes))
        if self.fairness_criterion == 'dp':
            self.weights = torch.zeros((1, self.n_classes))
        
        for epoch in range(epochs):
            self._train_epoch(epoch, train_loader, model, criterion)
            
            eval_start_time = time.time()
            eval_loss, eval_acc, eval_dcam, eval_dcaa, _, _  = self.evaluate(self.model, 
                                                                             test_loader, 
                                                                             self.criterion,
                                                                             epoch, 
                                                                             train=False,
                                                                             record=self.record,
                                                                             writer=writer
                                                                            )
            eval_end_time = time.time()
            print('[{}/{}] Method: {} '
                  'Test Loss: {:.3f} Test Acc: {:.2f} Test DCAM {:.2f} [{:.2f} s]'.format
                  (epoch + 1, epochs, self.method,
                   eval_loss, eval_acc, eval_dcam, (eval_end_time - eval_start_time)))
            
            if self.record:
                outputs, groups, labels = [], [], []
                for i, data in enumerate(test_loader):
                    inputs, _, group, target, idx = data
                    if self.cuda:
                        inputs = inputs.cuda(device=self.device)
                        label = target.cuda(device=self.device)
                        group = group.cuda(device=self.device)
                    output = self.model(inputs)
                    outputs.append(output)
                    groups.append(group)
                    labels.append(label)
                
                outputs = torch.cat(outputs)
                groups = torch.cat(groups)
                labels = torch.cat(labels)

                renyi = self.calculate_correlation(outputs, groups, labels, self.weights)
                writer.add_scalar('renyi', renyi.item(), epoch)
                
            if self.scheduler != None and 'Reduce' in type(self.scheduler).__name__:
                self.scheduler.step(eval_loss)
            else:
                self.scheduler.step()
        print('Training Finished!')        

    def calculate_correlation(self, outputs, groups, labels, weights):
        if self.fairness_criterion == 'dp':            
            assert (weights).shape == (1, self.n_classes)

            output_probs = torch.nn.Softmax(dim=None)(outputs) # n by c

            if self.n_groups == 2:
                s_tilde = ((2*groups-1).view(len(groups),1)) * (torch.ones_like(groups).view(len(groups),1)).expand(len(groups), self.n_classes)
                multiplier = -(weights**2)+weights*s_tilde
                assert (multiplier).shape == (len(groups), self.n_classes)

                sample_loss = torch.sum(multiplier*output_probs, dim=1)
                loss = torch.mean(sample_loss)

            else: 
                print('not implemented')

        if self.fairness_criterion == 'eo':
            assert (weights).shape == (self.n_classes, self.n_classes)
            loss = 0
            index_set = []
            for c in range(self.n_classes):
                index_set.append(torch.where(labels==c)[0])

            output_probs = torch.nn.Softmax(dim=None)(outputs) # n by c

            if self.n_groups == 2:
                s_tilde = ((2*groups-1).view(len(groups),1)) * (torch.ones_like(groups).view(len(groups),1)).expand(len(groups), self.n_classes)
                
                for c in range(self.n_classes):
                    if index_set[c] == []:
                        pass
                    else:
                        output_probs_c = output_probs[index_set[c]]
                        s_tilde_c = s_tilde[index_set[c]]
                        weights_c = weights[c]
                        multiplier_c = -(weights_c**2)+weights_c*s_tilde_c
                        assert (multiplier_c).shape == (len(index_set[c]), self.n_classes)

                        sample_loss_c = torch.sum(multiplier_c*output_probs_c, dim=1)
                        loss_c = torch.mean(sample_loss_c)
                        loss += loss_c

        return loss


    def _train_epoch(self, epoch, train_loader, model, criterion=None):
        model.train()
        
        running_acc = 0.0
        running_loss = 0.0
        batch_start_time = time.time()

        n_classes = train_loader.dataset.n_classes
        n_groups = train_loader.dataset.n_groups
        n_subgroups = n_classes * n_groups

        for i, data in enumerate(train_loader):
            # Get the inputs
        
            inputs, _, groups, targets, idx = data
            groups = groups.long()
            labels = targets.long()
            weights = self.weights
            
            if self.cuda:
                inputs = inputs.cuda(device=self.device)
                labels = labels.cuda(device=self.device)
                groups = groups.cuda(device=self.device)
                weights = weights.cuda(device=self.device)
                
                
            if self.data == 'jigsaw':
                input_ids = inputs[:, :, 0]
                input_masks = inputs[:, :, 1]
                segment_ids = inputs[:, :, 2]
                outputs = model(
                    input_ids=input_ids,
                    attention_mask=input_masks,
                    token_type_ids=segment_ids,
                    labels=labels,
                )[1] 
            else:
                outputs = model(inputs)

            if self.balanced:
                subgroups = groups * n_classes + labels
                group_map = (subgroups == torch.arange(n_subgroups).unsqueeze(1).long().cuda()).float()
                group_count = group_map.sum(1)
                group_denom = group_count + (group_count==0).float() # avoid nans
                loss = nn.CrossEntropyLoss(reduction='none')(outputs, labels)
                group_loss = (group_map @ loss.view(-1))/group_denom
                loss = torch.mean(group_loss)
            else:
                if criterion is not None:
                    loss = criterion(outputs, labels).mean()
                else:
                    loss = self.criterion(outputs, labels).mean()
            
            loss += self.lamb * self.calculate_correlation(outputs, groups, labels, weights)
            
            loss.backward()
            if self.data == 'jigsaw':
                torch.nn.utils.clip_grad_norm_(model.parameters(),self.max_grad_norm)
            self.optimizer.step()
            self.optimizer.zero_grad()
                
            running_loss += loss.item()
            running_acc += get_accuracy(outputs, labels)
            
            if i % self.term == self.term-1: # print every self.term mini-batches
                avg_batch_time = time.time()-batch_start_time
                print('[{}/{}, {:5d}] Method: {} Train Loss: {:.3f} Train Acc: {:.2f} '
                      '[{:.2f} s/batch]'.format
                      (epoch + 1, self.epochs, i+1, self.method, running_loss / self.term, running_acc / self.term,
                       avg_batch_time/self.term))

                running_loss = 0.0
                running_acc = 0.0
                batch_start_time = time.time()
            
        self.weights = self.update_weights(train_loader.dataset, self.bs, self.n_workers, model, weights) # implemented for each epoch
            
    def update_weights(self, dataset, bs, n_workers, model, weights):  
        model.eval()
        
        dataloader = DataLoader(dataset, batch_size=bs, shuffle=False,
                                num_workers=n_workers, pin_memory=True, drop_last=False)
        
        
        Y_prob_set = []
        Y_set = []
        S_set = []
        total = 0
        with torch.no_grad():
            for i, data in enumerate(dataloader):
                inputs, _, sen_attrs, targets, _ = data
                Y_set.append(targets) # sen_attrs = -1 means no supervision for sensitive group
                S_set.append(sen_attrs)

                if self.cuda:
                    inputs = inputs.cuda()
                    groups = sen_attrs.cuda()
                    targets = targets.cuda()
                if model != None:
                    if self.data == 'jigsaw':
                        input_ids = inputs[:, :, 0]
                        input_masks = inputs[:, :, 1]
                        segment_ids = inputs[:, :, 2]
                        outputs = model(
                            input_ids=input_ids,
                            attention_mask=input_masks,
                            token_type_ids=segment_ids,
                            labels=targets,
                        )[1] 
                    else:
                        outputs = model(inputs)
                    output_probs = torch.nn.Softmax(dim=None)(outputs) # n by c
                    Y_prob_set.append(output_probs)
                total+= inputs.shape[0]

        Y_set = torch.cat(Y_set).long().cuda()
        S_set = torch.cat(S_set).long().cuda()
        Y_prob_set = torch.cat(Y_prob_set) if len(Y_prob_set) != 0 else torch.zeros(0)
        
        
        index_set =[]
        for c in range(self.n_classes):
            index_set.append(torch.where(Y_set==c)[0])
        
        if self.n_groups == 2:
            S_set = S_set.view(len(S_set), 1)
            S_tilde_set = 2*S_set-1
            for c in range(self.n_classes):
                denominator = 2*torch.sum(Y_prob_set[index_set[c]], dim=0).view(1, self.n_classes)
                numerator = torch.sum(S_tilde_set[index_set[c]]*Y_prob_set[index_set[c]], dim=0).view(1, self.n_classes)
                weights[c] = numerator/denominator 
        return weights
