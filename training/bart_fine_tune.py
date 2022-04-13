import torch
from transformers import BartConfig, BartForConditionalGeneration, BartTokenizer
from transformers import get_cosine_with_hard_restarts_schedule_with_warmup, get_linear_schedule_with_warmup, \
    get_cosine_schedule_with_warmup

from datasets.spell.main.model.spellcheck_model import BART
from datasets.spell.main.training.data_processing import read_data
from datasets.spell.main.training.trainer_transformer_seq2seq import train_model

# PATH_PREFIX = '/Users/olegmelnikov/PycharmProjects/jb-spellchecker/'
PATH_PREFIX = '/home/ubuntu/omelnikov/grazie/spell/main/'



if __name__ == '__main__':
    train_data = read_data(gt_path=PATH_PREFIX + 'data/datasets/1blm/1blm.train.gt', noise_path=PATH_PREFIX + 'data/datasets/1blm/1blm.train.noise')
    val_data = read_data(gt_path=PATH_PREFIX + 'data/datasets/1blm/1blm.test.gt', noise_path=PATH_PREFIX + 'data/datasets/1blm/1blm.test.noise')

    tokenizer = BartTokenizer.from_pretrained('facebook/bart-base')
    model = BartForConditionalGeneration.from_pretrained('facebook/bart-base')

    # If needed take existing checkpoint
    checkpoint = PATH_PREFIX + 'training/checkpoints/bart-base_v1_4.pt'
    model.load_state_dict(torch.load(checkpoint))
    print('Model loaded from', checkpoint)

    device = torch.device('cuda:2' if torch.cuda.is_available() else 'cpu')
    model = model.to(device)

    model_name = 'bart-base'
    batch_size = 32
    num_epochs = 10
    st_epoch = 5
    print_n_batches = 4000
    num_sent = 1000000000
    model_version = 2
    test_mode = False
    train_data = train_data[:num_sent]
    num_batches_in_epoch = len(train_data) // batch_size

    optimizer = torch.optim.AdamW(params=model.parameters(), lr=0.00005)
    scheduler = get_cosine_with_hard_restarts_schedule_with_warmup(optimizer, num_batches_in_epoch * 1,
                                                                   num_batches_in_epoch * num_epochs, num_epochs - 1)

    print(f'Start training. Num epocs: {num_epochs}, batch size: {batch_size}, num sents: {len(train_data)}')
    train_model(model, tokenizer, optimizer, scheduler, train_data, val_data, batch_size, print_n_batches, num_epochs,
                st_epoch, model_name, BART, device, save_model=not test_mode, use_tensorboard=not test_mode,
                model_version=model_version, test_mode=test_mode)
