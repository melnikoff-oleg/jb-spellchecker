import random
from torch.utils.tensorboard import SummaryWriter
import torch
from transformers import BartConfig, BartForConditionalGeneration, BartTokenizer
from tqdm import tqdm
from transformers import get_linear_schedule_with_warmup, get_cosine_schedule_with_warmup
import datetime
import os

from grazie.spell.main.data.utils import get_texts_from_file
from grazie.spell.main.model.spellcheck_model import BART
from grazie.spell.main.evaluation.evaluate import evaluate

# PATH_PREFIX = '/Users/olegmelnikov/PycharmProjects/jb-spellchecker/'
PATH_PREFIX = '/home/ubuntu/omelnikov/grazie/spell/main/'


def train_model(model, tokenizer, train_data, val_data, num_epochs, batch_size, optimizer, scheduler,
                print_n_batches=2000, st_epoch=0, model_name='bart', device=torch.device('cuda'),
                save_model=False, use_tensorboard=False, model_version=0):

    # Init tensorboard for logs writing
    if use_tensorboard:
        tb = torch.utils.tensorboard.SummaryWriter(log_dir=f'{PATH_PREFIX}training/tensorboard_logs/{model_name}/v{model_version}/st_epoch:{st_epoch}_date:{datetime.datetime.now().strftime("%m-%Y-%H-%M")}')

    num_batches = (len(train_data) + batch_size - 1) // batch_size
    for epoch in tqdm(range(st_epoch, st_epoch + num_epochs), desc='Epochs', leave=True):
        model.train()
        epoch_loss = 0
        for i in tqdm(range(num_batches), position=0, leave=True, desc='Batches'):
            batch = train_data[i * batch_size: min(i * batch_size + batch_size, len(train_data))]
            prefix = [i[0] for i in batch]
            suffix = [i[1] for i in batch]
            encoder_input = tokenizer(prefix, return_tensors='pt', padding=True).to(device)
            decoder_input = tokenizer(suffix, return_tensors='pt', padding=True).to(device)
            result = model(**encoder_input, labels=decoder_input['input_ids'])
            loss = result.loss
            loss.backward()
            optimizer.step()
            scheduler.step()
            model.zero_grad()
            epoch_loss += loss.cpu().item()
            batch_ind = num_batches * epoch + i

            # Printing all the stats and writing to tensorboard
            if batch_ind % print_n_batches == 0:
                print(f'\nTrain loss on batch {batch_ind}: {loss.cpu().item() / batch_size}')
                print(f'Learning rate: {scheduler.get_last_lr()[0]}')
                if use_tensorboard:
                    tb.add_scalar('Learning rate', scheduler.get_last_lr()[0], batch_ind)
                    tb.add_scalar('Train loss on batch', loss.cpu().item() / batch_size, batch_ind)

                # Calculate loss on validation data
                model.eval()
                with torch.no_grad():
                    val_batches = 10
                    batches = list(range(0, (len(val_data) + batch_size - 1) // batch_size))
                    random.shuffle(batches)
                    val_loss = 0
                    num_objects = 0
                    for j in batches[:val_batches]:
                        batch = val_data[j * batch_size: min(j * batch_size + batch_size, len(val_data))]
                        num_objects += len(batch)
                        prefix = [k[0] for k in batch]
                        suffix = [k[1] for k in batch]
                        encoder_input = tokenizer(prefix, return_tensors='pt', padding=True).to(device)
                        decoder_input = tokenizer(suffix, return_tensors='pt', padding=True).to(device)
                        result = model(**encoder_input, labels=decoder_input['input_ids'])

                        loss = result.loss
                        val_loss += loss.cpu().item()

                    val_loss /= num_objects

                    result_ids = model.generate(tokenizer([val_data[0][0], val_data[1][0], val_data[2][0]], return_tensors='pt', padding=True).to(device)["input_ids"],
                        num_beams=5, min_length=5, max_length=500)

                    ans = tokenizer.batch_decode(result_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
                    ans_str = ''
                    for i in range(3):
                        ans_str += f'{val_data[i][0]} - Noised {i}\n{val_data[i][1]} - GT {i}\n{ans[i]} - Answer {i}'
                        if i < 2:
                            ans_str += '\n\n'

                    # Calculate metrics on test dataset
                    path_prefix = '/home/ubuntu/omelnikov/grazie/spell/main/'
                    bart_model = BART(model=model)
                    texts_gt, texts_noise = get_texts_from_file(path_prefix + 'data/datasets/bea/bea500.gt'), \
                                            get_texts_from_file(path_prefix + 'data/datasets/bea/bea500.noise')
                    evaluation_report = evaluate(bart_model, texts_gt, texts_noise)
                    metrics = ['Precision', 'Recall', 'F_0_5', 'Word-level accuracy', 'Broken tokenization cases']
                    if use_tensorboard:
                        tb.add_text('Test sentence rewriting', ans_str, batch_ind)
                        tb.add_scalar("Val loss", val_loss, batch_ind)
                        for metric in metrics:
                            tb.add_scalar(metric, evaluation_report['Metrics'][metric], batch_ind)

                model.train()

        if save_model:
            checkpoints_dir = f'{PATH_PREFIX}training/checkpoints/'
            if not os.path.exists(checkpoints_dir):
                os.makedirs(checkpoints_dir)
            model_path = f'{checkpoints_dir}{model_name}_v{model_version}_{epoch}.pt'
            torch.save(model.state_dict(), model_path)
            print('Model saved in', model_path)
        else:
            print('Model was not saved')

        print('Train loss on epoch', epoch, ':', epoch_loss / len(train_data))
        tb.add_scalar("Train loss on epoch", epoch_loss / len(train_data), epoch)

    tb.close()


def read_data(gt_path, noise_path):
    data = []
    with open(gt_path) as f:
        gt = f.readlines()
    with open(noise_path) as f:
        noise = f.readlines()
    for i, j in zip(noise, gt):
        data.append(tuple([i, j]))
    # for ind, i in enumerate(data):
    #     data[ind] = (i[0].replace(' ', '_'), i[1].replace(' ', '_'))

    return data

if __name__ == '__main__':
    train = read_data(gt_path=PATH_PREFIX + 'data/datasets/1blm/1blm.train.gt', noise_path=PATH_PREFIX + 'data/datasets/1blm/1blm.train.noise')
    val = read_data(gt_path=PATH_PREFIX + 'data/datasets/1blm/1blm.test.gt', noise_path=PATH_PREFIX + 'data/datasets/1blm/1blm.test.noise')

    tokenizer = BartTokenizer.from_pretrained('facebook/bart-base')
    model = BartForConditionalGeneration.from_pretrained('facebook/bart-base')

    # If needed take existing checkpoint
    # checkpoint = PATH_PREFIX + 'training/model_big_0_9.pt'
    # model.load_state_dict(torch.load(checkpoint))
    # print('Model loaded from', checkpoint)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = model.to(device)

    model_name = 'bart-base'
    batch_size = 32
    num_epochs = 10
    st_epoch = 0
    print_n_batches = 4000
    num_sent = 1000000000
    model_version = 0
    train = train[:num_sent]
    num_batches_in_epoch = len(train) // batch_size

    optimizer = torch.optim.AdamW(params=model.parameters(), lr=0.0001)
    # scheduler = get_linear_schedule_with_warmup(optimizer, num_batches_in_epoch, num_batches_in_epoch * num_epochs)
    scheduler = get_cosine_schedule_with_warmup(optimizer, num_batches_in_epoch * 1, num_batches_in_epoch * num_epochs)

    print(f'Start training. Num epocs: {num_epochs}, batch size: {batch_size}, num sents: {len(train)}')
    train_model(model, tokenizer, train, val, num_epochs, batch_size, optimizer, scheduler,
                print_n_batches=print_n_batches, st_epoch=st_epoch, model_name=model_name, device=device,
                save_model=True, use_tensorboard=True, model_version=model_version)
