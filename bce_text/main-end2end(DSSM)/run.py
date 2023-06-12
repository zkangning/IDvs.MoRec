import torch.optim as optim
import re
from pathlib import Path
from torch.utils.data import DataLoader
import numpy as np
from transformers import BertModel, BertTokenizer, BertConfig, \
    RobertaTokenizer, RobertaModel, RobertaConfig, \
    DebertaTokenizer, DebertaModel, DebertaConfig


from parameters import parse_args
from model import Model
from data_utils import read_news, read_news_bert, get_doc_input_bert, \
    read_behaviors, BuildTrainDataset, eval_model, get_user_embeddings, get_item_embeddings
from data_utils.utils import *
import random

import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

os.environ["TOKENIZERS_PARALLELISM"] = "false"


def train(args, use_modal, local_rank):
    if use_modal:
        if 'debertaV1' in args.bert_model_load:
            Log_file.info('load roberta model...')
            bert_model_load = '/yuanzheng/id_modal/pretrained_models/deberta/' + args.bert_model_load
            tokenizer = DebertaTokenizer.from_pretrained(bert_model_load)
            config = DebertaConfig.from_pretrained(bert_model_load, output_hidden_states=True)
            bert_model = DebertaModel.from_pretrained(bert_model_load, config=config)
        elif 'roberta' in args.bert_model_load:
            Log_file.info('load roberta model...')
            bert_model_load = '/yuanzheng/id_modal/pretrained_models/roberta/' + args.bert_model_load
            tokenizer = RobertaTokenizer.from_pretrained(bert_model_load)
            config = RobertaConfig.from_pretrained(bert_model_load, output_hidden_states=True)
            bert_model = RobertaModel.from_pretrained(bert_model_load, config=config)
        else:
            Log_file.info('load bert model...')
            bert_model_load = '/yuanzheng/id_modal/pretrained_models/bert/' + args.bert_model_load
            tokenizer = BertTokenizer.from_pretrained(bert_model_load)
            config = BertConfig.from_pretrained(bert_model_load, output_hidden_states=True)
            bert_model = BertModel.from_pretrained(bert_model_load, config=config)

        if 'tiny' in args.bert_model_load:
            pooler_para = [37, 38]
            args.word_embedding_dim = 128
        if 'mini' in args.bert_model_load:
            pooler_para = [69, 70]
            args.word_embedding_dim = 256
        if 'medium' in args.bert_model_load:
            pooler_para = [133, 134]
            args.word_embedding_dim = 512
        if 'base' in args.bert_model_load:
            pooler_para = [197, 198]
            args.word_embedding_dim = 768
        if 'large' in args.bert_model_load:
            pooler_para = [389, 390]
            args.word_embedding_dim = 1024

        for index, (name, param) in enumerate(bert_model.named_parameters()):
            if index < args.freeze_paras_before or index in pooler_para:
                param.requires_grad = False

        Log_file.info('read news...')
        item_id_to_dic, item_name_to_id = read_news_bert(
            os.path.join(args.root_data_dir, args.dataset, args.news), args, tokenizer)

        Log_file.info('read behaviors...')
        user_num, item_num, item_id_to_content, users_train, users_valid, train_pairs, valid_pairs, test_pairs, \
        users_history_for_valid, users_history_for_test = \
            read_behaviors(os.path.join(args.root_data_dir, args.dataset, args.behaviors),
                           item_id_to_dic, item_name_to_id, args.min_seq_len, args.max_seq_len, Log_file)

        Log_file.info('combine news information...')
        news_title, news_title_attmask, \
        news_abstract, news_abstract_attmask, \
        news_body, news_body_attmask = get_doc_input_bert(item_id_to_content, args)

        item_content = np.concatenate([
            x for x in
            [news_title, news_title_attmask,
             news_abstract, news_abstract_attmask,
             news_body, news_body_attmask]
            if x is not None], axis=1)
    else:
        item_id_to_dic, item_name_to_id = read_news(os.path.join(args.root_data_dir, args.dataset, args.news))

        Log_file.info('read behaviors...')
        user_num, item_num, item_id_to_content, users_train, users_valid, train_pairs, valid_pairs, test_pairs, \
        users_history_for_valid, users_history_for_test \
            = read_behaviors(os.path.join(args.root_data_dir, args.dataset, args.behaviors),
                             item_id_to_dic, item_name_to_id, args.min_seq_len, args.max_seq_len, Log_file)

        item_content = np.arange(item_num + 1)
        bert_model = None

    Log_file.info('build dataset...')
    train_dataset = BuildTrainDataset(user_history=users_train, train_pairs=train_pairs, item_num=item_num,
                                      item_content=item_content, neg_num=args.neg_num, use_modal=use_modal)

    Log_file.info('build DDP sampler...')
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)

    def worker_init_reset_seed(worker_id):
        initial_seed = torch.initial_seed() % 2 ** 31
        worker_seed = initial_seed + worker_id + dist.get_rank()
        random.seed(worker_seed)
        np.random.seed(worker_seed)

    Log_file.info('build dataloader...')
    train_dl = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.num_workers,
                          worker_init_fn=worker_init_reset_seed, pin_memory=True, sampler=train_sampler)

    Log_file.info('build model...')
    model = Model(args, user_num, item_num, use_modal, bert_model).to(local_rank)
    model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model).to(local_rank)
    # Log_file.info(model)

    if 'None' not in args.load_ckpt_name:
        Log_file.info('load ckpt if not None...')
        ckpt_path = get_checkpoint(model_dir, args.load_ckpt_name)
        checkpoint = torch.load(ckpt_path, map_location=torch.device('cpu'))
        Log_file.info('load checkpoint...')
        model.load_state_dict(checkpoint['model_state_dict'])
        Log_file.info(f"Model loaded from {ckpt_path}")
        start_epoch = int(re.split(r'[._-]', args.load_ckpt_name)[1])
        torch.set_rng_state(checkpoint['rng_state'])
        torch.cuda.set_rng_state(checkpoint['cuda_rng_state'])
        is_early_stop = False
    else:
        checkpoint = None
        ckpt_path = None
        start_epoch = 0
        is_early_stop = True

    model = DDP(model, device_ids=[local_rank], output_device=local_rank)

    if use_modal:
        bert_params = []
        recsys_params = []
        for index, (name, param) in enumerate(model.module.named_parameters()):
            if param.requires_grad:
                if 'bert_model' in name:
                    bert_params.append(param)
                else:
                    recsys_params.append(param)
        optimizer = optim.AdamW([
            {'params': bert_params, 'lr': args.fine_tune_lr, 'weight_decay': args.l2_weight},
            {'params': recsys_params, 'lr': args.lr, 'weight_decay': args.l2_weight}
        ])

        Log_file.info("***** {} parameters in bert, {} parameters in model *****".format(
            len(list(model.module.bert_encoder.text_encoders.title.bert_model.parameters())),
            len(list(model.module.parameters()))))

        for children_model in optimizer.state_dict()['param_groups']:
            Log_file.info("***** {} parameters have learning rate {}, weight_decay {} *****".format(
                len(children_model['params']), children_model['lr'], children_model['weight_decay']))

        model_params_require_grad = []
        model_params_freeze = []
        bert_params_require_grad = []
        bert_params_freeze = []
        for param_name, param_tensor in model.module.named_parameters():
            if param_tensor.requires_grad:
                model_params_require_grad.append(param_name)
                if 'bert_model' in param_name:
                    bert_params_require_grad.append(param_name)
            else:
                model_params_freeze.append(param_name)
                if 'bert_model' in param_name:
                    bert_params_freeze.append(param_name)
        Log_file.info("***** freeze parameters before {} in bert *****".format(args.freeze_paras_before))
        Log_file.info("***** model: {} parameters require grad, {} parameters freeze *****".format(
            len(model_params_require_grad), len(model_params_freeze)))
        Log_file.info("***** bert: {} parameters require grad, {} parameters freeze *****".format(
            len(bert_params_require_grad), len(bert_params_freeze)))
    else:
        optimizer = optim.AdamW(model.module.parameters(), lr=args.lr, weight_decay=args.l2_weight)

    if 'None' not in args.load_ckpt_name:
        optimizer.load_state_dict(checkpoint["optimizer"])
        Log_file.info(f"optimizer loaded from {ckpt_path}")

    total_num = sum(p.numel() for p in model.parameters())
    trainable_num = sum(p.numel() for p in model.parameters() if p.requires_grad)
    Log_file.info("##### total_num {} #####".format(total_num))
    Log_file.info("##### trainable_num {} #####".format(trainable_num))

    Log_file.info('\n')
    Log_file.info('Training...')
    next_set_start_time = time.time()
    max_epoch, early_stop_epoch = 0, args.epoch
    max_eval_value, early_stop_count = 0, 0

    steps_for_log, steps_for_test = para_and_log(model, len(train_pairs), args.batch_size, Log_file,
                                    logging_num=args.logging_num, testing_num=args.testing_num)

    Log_screen.info('{} train start'.format(args.label_screen))
    Log_file.info('')
    scaler = torch.cuda.amp.GradScaler()
    for ep in range(args.epoch):
        now_epoch = start_epoch + ep + 1
        Log_file.info('\n')
        Log_file.info('epoch {} start'.format(now_epoch))
        Log_file.info('')
        loss, batch_index, need_break = 0.0, 1, False
        model.train()
        train_dl.sampler.set_epoch(now_epoch)
        for data in train_dl:
            input_user, sample_items, bce_label = data
            input_user, sample_items, bce_label = input_user.to(local_rank).squeeze(), \
                                                  sample_items.to(local_rank), \
                                                  bce_label.to(local_rank)
            if use_modal:
                sample_items = sample_items.view(-1, sample_items.size(-1))
            else:
                sample_items = sample_items.view(-1)
            optimizer.zero_grad()
            with torch.cuda.amp.autocast():
                bz_loss = model(input_user, sample_items, bce_label)
                loss += bz_loss.data.float()
            scaler.scale(bz_loss).backward()
            scaler.step(optimizer)
            scaler.update()

            if torch.isnan(loss.data):
                need_break = True
                break
            if batch_index % steps_for_log == 0:
                Log_file.info('cnt: {}, Ed: {}, batch loss: {:.5f}, sum loss: {:.5f}'.format(
                    batch_index, batch_index * args.batch_size, loss.data / batch_index, loss.data))
            if not need_break and batch_index % steps_for_test == 0:
                Log_file.info('')
                max_eval_value, max_epoch, early_stop_epoch, early_stop_count, need_break, need_save = \
                    run_eval(now_epoch, max_epoch, early_stop_epoch, max_eval_value, early_stop_count,
                             model, item_content, users_history_for_valid, valid_pairs, 512, user_num, item_num,
                             use_modal, args.mode, is_early_stop, local_rank)
                if need_save and dist.get_rank() == 0:
                    save_label = str(int(batch_index / steps_for_test))
                    save_model(now_epoch, model, model_dir, optimizer, torch.get_rng_state(),
                               torch.cuda.get_rng_state(), save_label, Log_file)
                model.train()
            batch_index += 1
        Log_file.info('')
        next_set_start_time = report_time_train(batch_index, now_epoch, loss, next_set_start_time, start_time, Log_file)
        Log_screen.info('{} training: epoch {}/{}'.format(args.label_screen, now_epoch, args.epoch))
        if need_break:
            break
    if dist.get_rank() == 0:
        save_model(now_epoch, model, model_dir, optimizer, torch.get_rng_state(),
                   torch.cuda.get_rng_state(), save_label, Log_file)
    Log_file.info('\n')
    Log_file.info('%' * 90)
    Log_file.info(' max eval Hit10 {:0.5f}  in epoch {}'.format(max_eval_value * 100, max_epoch))
    Log_file.info(' early stop in epoch {}'.format(early_stop_epoch))
    Log_file.info('the End')
    Log_screen.info('{} train end in epoch {}'.format(args.label_screen, early_stop_epoch))


def run_eval(now_epoch, max_epoch, early_stop_epoch, max_eval_value, early_stop_count,
             model, item_content, user_history, eval_pairs, batch_size,
             user_num, item_num, use_modal, mode, is_early_stop, local_rank):
    eval_start_time = time.time()
    Log_file.info('Validating...')
    user_embeddings = get_user_embeddings(model, user_num, batch_size, args, local_rank)
    item_embeddings = get_item_embeddings(model, item_content, batch_size, args, use_modal, local_rank)
    eval_Hit10 = eval_model(model, user_history, eval_pairs, user_embeddings, item_embeddings, batch_size,
                            args, item_num, Log_file, mode, local_rank)
    report_time_eval(eval_start_time, Log_file)

    Log_file.info('')
    need_break = False
    need_save = False
    if eval_Hit10 > max_eval_value:
        max_eval_value = eval_Hit10
        max_epoch = now_epoch
        early_stop_count = 0
        need_save = True
    else:
        early_stop_count += 1
        if early_stop_count > 10:
            if is_early_stop:
                need_break = True
            early_stop_epoch = now_epoch
    return max_eval_value, max_epoch, early_stop_epoch, early_stop_count, need_break, need_save


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


if __name__ == "__main__":
    args = parse_args()
    local_rank = args.local_rank
    torch.cuda.set_device(local_rank)
    dist.init_process_group(backend='nccl')
    setup_seed(12345)

    if 'modal' in args.item_tower:
        is_use_modal = True
        model_load = args.bert_model_load
        dir_label = str(args.item_tower) + f'_{model_load}_freeze_{args.freeze_paras_before}'
    else:
        is_use_modal = False
        model_load = 'id'
        dir_label = str(args.item_tower)

    log_paras = f'{model_load}_dnn_{args.dnn_layers}_bs_{args.batch_size}' \
                f'_ed_{args.embedding_dim}_lr_{args.lr}' \
                f'_L2_{args.l2_weight}_dp_{args.drop_rate}_Flr_{args.fine_tune_lr}'
    model_dir = os.path.join('./checkpoint_' + dir_label, 'cpt_' + log_paras)
    time_run = time.strftime('-%Y%m%d-%H%M%S', time.localtime())
    args.label_screen = args.label_screen + time_run
    Log_file, Log_screen = setuplogger(dir_label, log_paras, time_run, args.mode, dist.get_rank())

    Log_file.info(args)
    if not os.path.exists(model_dir):
        Path(model_dir).mkdir(parents=True, exist_ok=True)

    start_time = time.time()
    if 'train' in args.mode:
        train(args, is_use_modal, local_rank)

    end_time = time.time()
    hour, minu, secon = get_time(start_time, end_time)
    Log_file.info("##### (time) all: {} hours {} minutes {} seconds #####".format(hour, minu, secon))