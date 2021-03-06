import sys

import features
import dataset
import os
import numpy as np
import boto3
import sqlite3
from enum import Enum

extractor = features.PEFeatureExtractor(print_feature_warning=False)
reader = dataset.LMDBReader(path="./dataset/ember_features/data.mdb", postproc_func=dataset.features_postproc_func)
s3 = boto3.client('s3')

train_amount = 50000
test_amount = 25000

feat_size = 2381  # From EMBER feature digest, DO NOT CHANGE!
label_size = 15


def main():
    np.set_printoptions(threshold=sys.maxsize)
    _, feats = get_labels_and_features(1, 0)
    print(feats[0][626])
    # save_npz("dataset/train_set.npz", train_amount, 0)
    # save_npz("dataset/test_set.npz", test_amount, train_amount)


# Saves a numpy file of
def save_npz(path, batch_amount, offset):
    """
    Creates and saves a numpy file with a labeled feature set from data within meta.db and data.mdb.

    :param path: File path to save the numpy file to.
    :param batch_amount: How many entries to pull from databases and save.
    :param offset: Offset within databases to start pulling entries from.
    """
    print(f'Extracting labels for {path}...')
    labels, feats = get_labels_and_features(batch_amount, offset, only_malware=False)

    # Modifying labels to only contain is_malware
    labels = labels[:, 1]

    labels = labels.astype(float)

    np.savez(path, labels=labels, features=feats)


def extract_preprocessed_features(file_id):
    """
    Returns the preprocessed features, i.e. features from data.mdb, of the corresponding file hash.
    Returns None if no features for that given file could be found.

    :param file_id: Hash of the file to get features of.
    """
    val = reader(file_id)
    if val is None:
        return None
    else:
        return val


def get_labels_and_features(amount, offset, only_malware=False, only_goodware=False):
    """
    Returns a set of labels from meta.db and a set of corresponding features from data.mdb.

    :param amount: Specifies how many labels to extract.
    :param offset: Offset within meta.db to start pulling from.
    :param only_malware: Determines whether all labels are of malware instances. Takes precidence over only_goodware.
    :param only_goodware: Determines whether all labels are of benign file instances.
    """
    con = sqlite3.connect('./dataset/ember_features/meta.db')
    cur = con.cursor()
    labels = np.empty((amount, label_size), dtype=str)
    feats = np.empty((amount, feat_size))
    count = 0
    # Iteration necessary because not all entries have features
    while count < amount:
        if only_malware:
            cur.execute('SELECT * FROM meta WHERE is_malware = 1 LIMIT ? OFFSET ?',
                        (amount - count, offset + count))
        elif only_goodware:
            cur.execute('SELECT * FROM meta WHERE is_malware = 0 LIMIT ? OFFSET ?',
                        (amount - count, offset + count))
        else:
            cur.execute('SELECT * FROM meta LIMIT ? OFFSET ?', (amount - count, offset + count))

        new_labels = cur.fetchall()

        # Trim featureless entries
        for i, entry in enumerate(new_labels):
            m_hash = entry[0]
            new_features = extract_preprocessed_features(m_hash)
            if new_features is not None and not np.isnan(new_features.any()):
                labels[count] = np.array(entry).reshape(1, label_size)
                feats[count] = new_features.reshape((1, feat_size))
                count += 1
                if count % 20 == 0:
                    printProgressBar(count, amount, printEnd='')

    printProgressBar(amount, amount, printEnd='\r\n')

    assert labels.shape == (amount, label_size)
    assert feats.shape == (amount, feat_size)

    return labels, feats


# Used to download PE files online - not used
def s3_download_PE(file_id):
    bucket = 'sorel-20m'
    obj_path = '09-DEC-2020/binaries'
    local_path = 'D:\ece5900_malware_binaries'

    s3_file = obj_path + '/' + file_id
    local_file = local_path + '\\' + file_id

    print(f'Downloading: {s3_file} to {local_file}')

    s3.download_file(bucket, s3_file, local_file)


# Print iterations progress from https://stackoverflow.com/questions/3173320/text-progress-bar-in-terminal-with-block-characters
def printProgressBar(iteration, total, prefix='', suffix='', decimals=1, length=100, fill='???', printEnd="\r"):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : character length of bar (Int)
        fill        - Optional  : bar fill character (Str)
        printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=printEnd)


if __name__ == '__main__':
    main()
