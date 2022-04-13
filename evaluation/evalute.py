import json
import torch
from typing import List, Dict
import random
import datetime
import os
from tqdm import tqdm

from data_utils.utils import get_texts_from_file
from model.spellcheck_model import SpellCheckModelBase

# one can make saving to file through decorator

PATH_PREFIX = '/home/ubuntu/omelnikov/grazie/spell/main/'
# PATH_PREFIX = '/Users/olegmelnikov/PycharmProjects/jb-spellchecker/grazie/spell/main/'


def evaluate(model: SpellCheckModelBase, texts_gt: List[str], texts_noise: List[str], exp_save_dir: str = None) -> Dict:
    tp, fp_1, fp_2, tn, fn = 0, 0, 0, 0, 0
    broken_tokenization_cases = 0
    fp_1_examples, fp_2_examples, fn_examples = [], [], []

    # Prepare folder and file to save info
    if exp_save_dir is not None:
        if not os.path.exists(exp_save_dir):
            os.makedirs(exp_save_dir)
        open(exp_save_dir + 'result.txt', 'w').close()

    # Iterating over all texts, comparing corrected version to gt
    for text_gt, text_noise in tqdm(zip(texts_gt, texts_noise), total=len(texts_gt)):
        text_res = model.correct(text_noise)
        words_gt, words_noise, words_res = text_gt.split(' '), text_noise.split(' '), text_res.split(' ')

        # If tokenization not preserved, then do nothing
        broken_tokenization = False
        if len(words_res) != len(words_noise):
            if exp_save_dir is not None:
                with open(exp_save_dir + 'result.txt', 'a+') as result_file:
                    result_file.write(f'Tokenization not preserved\n')
            broken_tokenization_cases += 1
            real_res = text_res
            text_res = text_noise
            words_res = words_noise
            broken_tokenization = True

        cur_tp, cur_fp_1, cur_fp_2, cur_tn, cur_fn = 0, 0, 0, 0, 0
        for word_gt, word_init, word_res in zip(words_gt, words_noise, words_res):
            word_report = {'Text noise': text_noise, 'Word noise': word_init, 'Word gt': word_gt, 'Word res': word_res}
            if word_init == word_gt:
                if word_res == word_gt:
                    cur_tn += 1
                else:
                    cur_fp_1 += 1
                    fp_1_examples.append(word_report)
            else:
                if word_res == word_gt:
                    cur_tp += 1
                else:
                    if word_res == word_init:
                        cur_fn += 1
                        fn_examples.append(word_report)
                    else:
                        cur_fp_2 += 1
                        fp_2_examples.append(word_report)

        # Writing info for current text
        if exp_save_dir is not None:
            with open(exp_save_dir + 'result.txt', 'a+') as result_file:
                result_file.write(f'TP: {cur_tp}, FP_1: {cur_fp_1}, FP_2: {cur_fp_2}, FN: {cur_fn}, TN: {cur_tn}\n')

        # Updating global tp, fp, ...
        tp, fp_1, fp_2, tn, fn = tp + cur_tp, fp_1 + cur_fp_1, fp_2 + cur_fp_2, tn + cur_tn, fn + cur_fn

        # Return text_res to real value for writing to file
        if broken_tokenization:
            text_res = real_res

        # Writing correction results for current text
        if exp_save_dir is not None:
            with open(exp_save_dir + 'result.txt', 'a+') as result_file:
                result_file.write(f'{text_noise} - Noised\n{text_gt} - GT\n{text_res} - Result\n\n')

    # Calculating metrics
    word_level_accuracy = round((tp + tn) / (tp + fp_1 + fp_2 + tn + fn), 2)
    precision = round(tp / (tp + fp_1 + fp_2), 2) if (tp + fp_1 + fp_2) > 0 else 0
    recall = round(tp / (tp + fn), 2)
    f_0_5 = round((1 + 0.5 ** 2) * precision * recall / ((precision * 0.5 ** 2) + recall), 2) \
        if (precision > 0 or recall > 0) else 0
    broken_tokenization_cases = round(broken_tokenization_cases / len(texts_gt), 2)

    # Leave at most 3 random examples of each mistake
    sample = lambda array: random.sample(array, min(len(array), 3))
    fp_1_examples, fp_2_examples, fn_examples = sample(fp_1_examples), sample(fp_2_examples), sample(fn_examples)

    # Collecting all evaluation info to one json
    report = {
        'Date': datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
        'Model': str(model),
        'Metrics': {
            'Precision': precision,
            'Recall': recall,
            'F_0_5': f_0_5,
            'Word-level accuracy': word_level_accuracy,
            'Broken tokenization cases': broken_tokenization_cases,
        },
        'Mistakes examples': {
            'Wrong correction of real mistake': fp_2_examples,
            'No mistake, but model corrected': fp_1_examples,
            'Not found mistake': fn_examples
        }
    }

    # Saving evaluation report
    if exp_save_dir is not None:
        with open(exp_save_dir + 'report.json', 'w') as result_file:
            json.dump(report, result_file, indent=4)

    # Printing report
    print(f'\nEvaluation metrics:\n\n{report["Metrics"]}')

    return report


# def evaluation_test():
#     path_prefix = '/home/ubuntu/omelnikov/grazie/spell/main/'
#     d_model = 256
#     checkpoint = 'training/model_big_0_9.pt'
#     model = CharBasedTransformerChecker(config={'d_model': d_model, 'encoder_layers': 6, 'decoder_layers': 6,
#                                          'encoder_attention_heads': 8, 'decoder_attention_heads': 8,
#                                          'encoder_ffn_dim': d_model * 4, 'decoder_ffn_dim': d_model * 4},
#                                  checkpoint=path_prefix + checkpoint)
#     texts_gt, texts_noise = get_texts_from_file(path_prefix + 'data/datasets/bea/bea50.gt'), \
#                             get_texts_from_file(path_prefix + 'data/datasets/bea/bea50.noise')
#
#     evaluate(model, texts_gt, texts_noise, path_prefix + 'data/experiments/char_based_transformer_big_10_epochs_test/')
#


if __name__ == '__main__':
    path_prefix = '/home/ubuntu/omelnikov/grazie/spell/main/'
    texts_gt, texts_noise = get_texts_from_file(path_prefix + 'data/datasets/bea/bea500.gt'), \
                            get_texts_from_file(path_prefix + 'data/datasets/bea/bea500.noise')

    # bart-base
    # checkpoint = 'training/checkpoints/bart-base_v0_3.pt'
    # model = BART(checkpoint=path_prefix + checkpoint, device=torch.device('cuda:3'))
    # evaluate(model, texts_gt, texts_noise, path_prefix + 'data/experiments/bart-base_v0_3/')

    # bart sep mask
    # model_name = 'bart-sep-mask_v1_3'
    # checkpoint = f'training/checkpoints/{model_name}'
    # model = BertBartChecker(checkpoint=path_prefix + checkpoint + '.pt', device=torch.device('cuda:1'))
    # evaluate(model, texts_gt, texts_noise, path_prefix + f'data/experiments/bert_detector_{model_name}/')

    # bart mask word 0 3 4 5 6 7
    # checkpoint = 'training/checkpoints/bart-mask-word_v0_2.pt'
    # model = MaskWordBART(checkpoint=path_prefix + checkpoint, device=torch.device('cuda:0'))
    # evaluate(model, texts_gt, texts_noise, path_prefix + 'data/experiments/bart-mask-word_v0_2/')


    # old BART + lev
    # model = three_part_model_train()
    # evaluate(model, texts_gt, texts_noise, path_prefix + 'data/experiments/old_bart_lev/')
