from __future__ import print_function
from collections import defaultdict
import time
from utils import get_accuracy
import trainer
import torch
import torch.nn as nn

class Trainer(trainer.GenericTrainer):
    def __init__(self, args, **kwargs):
        super().__init__(args=args, **kwargs)
        self.lamb = args.lamb
        self.fairness_criterion = args.fairness_criterion
        assert self.fairness_criterion == 'dca' #not implemented for other criteria
        
    def train(self, train_loader, test_loader, epochs, criterion=None, writer=None):
        global loss_set
        model = self.model
        model.train()
            
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
                self.evaluate(self.model, train_loader, self.criterion, epoch, 
                              train=True, 
                              record=self.record,
                              writer=writer
                             )
                n_classes = train_loader.dataset.n_classes
                reg = self._calculate_reg(self.model, train_loader)
                regs = {}
                for l in range(n_classes):
                    regs[f'l{l}'] = reg[l]
                writer.add_scalars('regs', regs, epoch)

            if self.scheduler != None and 'Reduce' in type(self.scheduler).__name__:
                self.scheduler.step(eval_loss)
            else:
                self.scheduler.step()
        print('Training Finished!')        

    def _calculate_reg(self, model, train_loader):
        total = 0
        n_classes = train_loader.dataset.n_classes
        n_groups = train_loader.dataset.n_groups
        n_subgroups = n_classes * n_groups

        group_total_denom = torch.zeros((n_groups, n_classes)).cuda()
        group_total_loss = torch.zeros((n_groups, n_classes)).cuda()
        for i, data in enumerate(train_loader):
            # Get the inputs
            inputs, _, groups, targets, idx = data
            labels = targets
            if self.cuda:
                inputs = inputs.cuda(device=self.device)
                labels = labels.cuda(device=self.device)
                groups = groups.cuda(device=self.device)

            with torch.no_grad():
                outputs = model(inputs)
                loss = nn.CrossEntropyLoss(reduction='none')(outputs, labels)

                subgroups = groups * n_classes + labels
                group_map = (subgroups == torch.arange(n_subgroups).unsqueeze(1).long().cuda()).float()
                group_loss = (group_map @ loss.view(-1))
                group_loss_matrix = group_loss.reshape(n_groups, n_classes)
                group_total_loss += group_loss_matrix

                group_count = group_map.sum(1)
                group_denom = group_count + (group_count==0).float() # avoid nans
                group_denom = group_denom.reshape(n_groups, n_classes)
                group_total_denom += group_denom
        
        group_total_loss /= group_total_denom
        abs_group_loss_diff = torch.abs(group_total_loss - group_total_loss.mean(dim=0))
        return abs_group_loss_diff.mean(dim=0).cpu().numpy()
        

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
            labels = targets
            if self.cuda:
                inputs = inputs.cuda(device=self.device)
                labels = labels.cuda(device=self.device)
                groups = groups.cuda(device=self.device)
                
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
            
            if self.fairness_criterion == 'dca':
                def closure_DCA(inputs, groups, labels, model):
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
                        outputs = model(inputs) # 128 by 2                    
                    
                    subgroups = groups * n_classes + labels
                    group_map = (subgroups == torch.arange(n_subgroups).unsqueeze(1).long().cuda()).float()
                    group_count = group_map.sum(1)
                    group_denom = group_count + (group_count==0).float() # avoid nans
                    loss = nn.CrossEntropyLoss(reduction='none')(outputs, labels)
                    group_loss = (group_map @ loss.view(-1))/group_denom
                    group_loss_matrix = group_loss.reshape(n_groups, n_classes)
                    abs_group_loss_diff = torch.abs(group_loss_matrix - group_loss_matrix.mean(dim=0))
                    DCA_reg = torch.mean(abs_group_loss_diff)
                    return DCA_reg
                
                loss += self.lamb*closure_DCA(inputs, groups, labels, model)
            
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