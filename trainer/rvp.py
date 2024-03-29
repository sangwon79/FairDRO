from __future__ import print_function
from collections import defaultdict

import copy
import time
from utils import get_accuracy
import trainer
import torch
import numpy as np


class Trainer(trainer.GenericTrainer):
    def __init__(self, args, **kwargs):
        super().__init__(args=args, **kwargs)
        self.train_criterion = torch.nn.CrossEntropyLoss(reduction='none')
        
        self.data = args.dataset
        self.seed = args.seed
        self.rho = args.rho
        
    def train(self, train_loader, test_loader, epochs, criterion=None, writer=None):
        
        global loss_set
        model = self.model
        model.train()

        for epoch in range(epochs):
            self._train_epoch(epoch, train_loader, model, criterion)            
            eval_start_time = time.time()
            eval_loss, eval_acc, eval_deom, eval_deoa, _, _  = self.evaluate(self.model, 
                                                                             test_loader, 
                                                                             self.criterion,
                                                                             epoch, 
                                                                             train=False,
                                                                             record=self.record,
                                                                             writer=writer
                                                                            )
            eval_end_time = time.time()
            print('[{}/{}] Method: {} '
                  'Test Loss: {:.3f} Test Acc: {:.2f} Test DEOM {:.2f} [{:.2f} s]'.format
                  (epoch + 1, epochs, self.method,
                   eval_loss, eval_acc, eval_deom, (eval_end_time - eval_start_time)))
            
            if self.scheduler != None and 'Reduce' in type(self.scheduler).__name__:
                self.scheduler.step(eval_loss)
            else:
                self.scheduler.step()
                  
        print('Training Finished!')        

    def _train_epoch(self, epoch, train_loader, model, criterion=None):
        model.train()
        
        running_acc = 0.0
        running_loss = 0.0
        batch_start_time = time.time()
        n_classes = train_loader.dataset.n_classes
        n_groups = train_loader.dataset.n_groups
        n_subgroups = n_classes * n_groups
        
        total_loss = torch.zeros(n_subgroups).cuda(device=self.device)
        
        idxs = np.array([i * n_classes for i in range(n_groups)])            
        for i, data in enumerate(train_loader):
            # Get the inputs
            inputs, _, groups, targets, idx = data
            labels = targets
            
#             if self.uc:
#                 groups_prob = groups
#                 groups = torch.distributions.categorical.Categorical(groups_prob).sample()
            
            if self.cuda:
                inputs = inputs.cuda(device=self.device)
                labels = labels.cuda(device=self.device)
                groups = groups.cuda(device=self.device)
                
            subgroups = groups * n_classes + labels
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

            if criterion is not None:
                loss = criterion(outputs, labels)
            else:
                loss = self.train_criterion(outputs, labels)

            # calculate the balSampling losses
            group_map = (subgroups == torch.arange(n_subgroups).unsqueeze(1).long().cuda()).float()
            group_count = group_map.sum(1)
            group_denom = group_count + (group_count==0).float() # avoid nans
            group_loss = (group_map @ loss.view(-1))/group_denom
            avg_group_loss = group_loss.sum() / n_subgroups
            
            var_loss = 0
            idxs = np.array([i * n_classes for i in range(n_groups)])            
            for l in range(n_classes):
                label_group_loss = group_loss[idxs+l]
                var_loss += torch.sqrt(label_group_loss.var() * self.rho / n_groups)
            var_loss /= n_classes        
            
            total_loss = avg_group_loss + var_loss
            
            self.optimizer.zero_grad()
            total_loss.backward()                
            if self.data == 'jigsaw':
                torch.nn.utils.clip_grad_norm_(model.parameters(),self.max_grad_norm)
            self.optimizer.step()
                
            running_loss += total_loss.item()
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