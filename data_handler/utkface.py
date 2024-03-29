from collections import defaultdict
from os.path import join
from PIL import Image
from utils import list_files
from natsort import natsorted
import random
import numpy as np
from torchvision import transforms
from data_handler import GenericDataset
from data_handler.utils import get_mean_std

class UTKFaceDataset(GenericDataset):
    label = 'age'
    sensi = 'race'
    fea_map = {
        'age' : 0,
        'gender' : 1,
        'race' : 2
    }
    n_map = {
        'age' : 100, # will be changed if the function '_transorm_age' is called
        'gender' : 2,
        'race' : 4
    }
    mean, std = get_mean_std('utkface')

    train_transform = transforms.Compose(
        [transforms.Resize((256, 256)),
         transforms.RandomCrop(224),
         transforms.RandomHorizontalFlip(),
         transforms.ToTensor(),
         transforms.Normalize(mean=mean, std=std)]
    )

    test_transform = transforms.Compose(
        [transforms.Resize((224, 224)),
         transforms.ToTensor(),
         transforms.Normalize(mean=mean, std=std)]
    )
    name = 'utkface'
    def __init__(self, **kwargs):
        
        transform = self.train_transform if kwargs['split'] == 'train' else self.test_transform

        GenericDataset.__init__(self, transform=transform, **kwargs)
#         SSLDataset.__init__(self, transform=transform, **kwargs)
        
        filenames = list_files(self.root, '.jpg')
        filenames = natsorted(filenames)
        self._data_preprocessing(filenames)
        self.n_groups = self.n_map[self.sensi]
        self.n_classes = self.n_map[self.label]        
        
        random.seed(1) # we want the same train / test set, so fix the seed to 1
        random.shuffle(self.features)
        
        train, test = self._make_data(self.features, self.n_groups, self.n_classes)
        self.features = train if self.split == 'train' else test
        
        self.n_data, self.idxs_per_group = self._data_count(self.features, self.n_groups, self.n_classes)
        
#         self.weights = self._make_weights()
                
    def __getitem__(self, index):
        s, l, img_name = self.features[index]
        
        image_path = join(self.root, img_name)
        image = Image.open(image_path, mode='r').convert('RGB')

        if self.transform:
            image = self.transform(image)
            
        return image, 1, np.float32(s), np.int64(l), index

    # five functions below preprocess UTKFace dataset
    def _data_preprocessing(self, filenames):
        filenames = self._delete_incomplete_images(filenames)
        filenames = self._delete_others_n_age_filter(filenames)
        self.features = [] 
        for filename in filenames:
            s, y = self._filename2SY(filename)
            self.features.append([s, y, filename])

    def _filename2SY(self, filename):        
        tmp = filename.split('_')
        sensi = int(tmp[self.fea_map[self.sensi]])
        label = int(tmp[self.fea_map[self.label]])
        if self.sensi == 'age':
            sensi = self._transform_age(sensi)
        if self.label == 'age':
            label = self._transform_age(label)
        return int(sensi), int(label)
        
    def _transform_age(self, age):
        if age<20:
            label = 0
        elif age<40:
            label = 1
        else:
            label = 2
        return label         

    def _delete_incomplete_images(self, filenames):
        filenames = [image for image in filenames if len(image.split('_')) == 4]
        return filenames

    def _delete_others_n_age_filter(self, filenames):
        filenames = [image for image in filenames
                            if ((image.split('_')[self.fea_map['race']] != '4'))]
        ages = [self._transform_age(int(image.split('_')[self.fea_map['age']])) for image in filenames]
        self.n_map['age'] = len(set(ages))
        return filenames

