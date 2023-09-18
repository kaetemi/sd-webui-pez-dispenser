import time
import copy
import open_clip
import torch
from modules import shared

class State:
    installed = False

state = State()

try:
    from sentence_transformers.util import (semantic_search, dot_score, normalize_embeddings)
    state.installed = True
except:
    print("pez-dispenser error: No sentence_transformers package installed")


def nn_project(curr_embeds, embedding_layer, print_hits=False):
    with torch.no_grad():
        bsz,seq_len,emb_dim = curr_embeds.shape
        
        # Using the sentence transformers semantic search which is 
        # a dot product exact kNN search between a set of 
        # query vectors and a corpus of vectors
        curr_embeds = curr_embeds.reshape((-1,emb_dim))
        curr_embeds = normalize_embeddings(curr_embeds) # queries

        embedding_matrix = embedding_layer.weight
        embedding_matrix = normalize_embeddings(embedding_matrix)
        
        hits = semantic_search(curr_embeds, embedding_matrix, 
                                query_chunk_size=curr_embeds.shape[0], 
                                top_k=1,
                                score_function=dot_score)

        if print_hits:
            all_hits = []
            for hit in hits:
                all_hits.append(hit[0]["score"])
            print(f"mean hits:{mean(all_hits)}")
        
        nn_indices = torch.tensor([hit[0]["corpus_id"] for hit in hits], device=curr_embeds.device)
        nn_indices = nn_indices.reshape((bsz,seq_len))

        projected_embeds = embedding_layer(nn_indices)

    return projected_embeds, nn_indices


def decode_ids(input_ids, tokenizer, by_token=False):
    input_ids = input_ids.detach().cpu().numpy()

    texts = []

    if by_token:
        for input_ids_i in input_ids:
            curr_text = []
            for tmp in input_ids_i:
                curr_text.append(tokenizer.decode([tmp]))

            texts.append("|".join(curr_text))
    else:
        for input_ids_i in input_ids:
            texts.append(tokenizer.decode(input_ids_i))

    return texts


def get_target_feature_images(model, preprocess, device, target_images):
    with torch.no_grad():
        curr_images = [preprocess(i).unsqueeze(0) for i in target_images]
        curr_images = torch.concatenate(curr_images).to(device)
        all_target_features = model.encode_image(curr_images)

    return all_target_features


def get_target_feature_prompts(model, tokenizer_funct, device, target_prompts):
    texts = tokenizer_funct(target_prompts).to(device)
    all_target_features = model.encode_text(texts)

    return all_target_features


def initialize_prompt(tokenizer, token_embedding, device, prompt_len, prompt_bs):

    # randomly optimize prompt embeddings
    prompt_ids = torch.randint(len(tokenizer.encoder), (prompt_bs, prompt_len)).to(device)
    prompt_embeds = token_embedding(prompt_ids).detach()
    prompt_embeds.requires_grad = True

    # initialize the template
    template_text = "{}"
    padded_template_text = template_text.format(" ".join(["<start_of_text>"] * prompt_len))
    dummy_ids = tokenizer.encode(padded_template_text)

    # -1 for optimized tokens
    dummy_ids = [i if i != 49406 else -1 for i in dummy_ids]
    dummy_ids = [49406] + dummy_ids + [49407]
    dummy_ids += [0] * (77 - len(dummy_ids))
    dummy_ids = torch.tensor([dummy_ids] * prompt_bs).to(device)

    # for getting dummy embeds; -1 won't work for token_embedding
    tmp_dummy_ids = copy.deepcopy(dummy_ids)
    tmp_dummy_ids[tmp_dummy_ids == -1] = 0
    dummy_embeds = token_embedding(tmp_dummy_ids).detach()
    dummy_embeds.requires_grad = False
    
    return prompt_embeds, dummy_embeds, dummy_ids


def encode_text_embedding(model, text_embedding, ids, avg_text=False):
    cast_dtype = model.transformer.get_cast_dtype()

    x = text_embedding + model.positional_embedding.to(cast_dtype)
    x = x.permute(1, 0, 2)  # NLD -> LND
    x = model.transformer(x, attn_mask=model.attn_mask)
    x = x.permute(1, 0, 2)  # LND -> NLD
    x = model.ln_final(x)

    # x.shape = [batch_size, n_ctx, transformer.width]
    # take features from the eot embedding (eot_token is the highest number in each sequence)
    if avg_text:
        x = x[torch.arange(x.shape[0]), :ids.argmax(dim=-1)]
        x[:, 1:-1]
        x = x.mean(dim=1) @ model.text_projection
    else:
        x = x[torch.arange(x.shape[0]), ids.argmax(dim=-1)] @ model.text_projection

    return x
    
def forward_text_embedding(model, embeddings, ids, image_features, avg_text=False, return_feature=False):
    text_features = encode_text_embedding(model, embeddings, ids, avg_text=avg_text)

    if return_feature:
        return text_features

    # normalized features
    image_features = image_features / image_features.norm(dim=1, keepdim=True)
    text_features = text_features / text_features.norm(dim=1, keepdim=True)

    # cosine similarity as logits
    # logit_scale = model.logit_scale.exp()
    logits_per_image = image_features @ text_features.t()
    logits_per_text = logits_per_image.t()

    # shape = [global_batch_size, global_batch_size]
    return logits_per_image, logits_per_text


def optimize_prompt_loop(model, tokenizer, token_embedding, all_target_features, device, prompt_len, opt_iters, lr, weight_decay, prompt_bs, print_step, batch_size, on_progress, progress_steps, progress_args):
    # initialize prompt
    prompt_embeds, dummy_embeds, dummy_ids = initialize_prompt(tokenizer, token_embedding, device, prompt_len, prompt_bs)
    p_bs, p_len, p_dim = prompt_embeds.shape

    # get optimizer
    input_optimizer = torch.optim.AdamW([prompt_embeds], lr=lr, weight_decay=weight_decay)

    best_sim = 0
    best_text = ""
    
    for step in range(opt_iters):
        if shared.state.interrupted:
            break
        if not on_progress is None:
            for progress_step in progress_steps:
                if step % progress_step == 0:
                    on_progress(step, opt_iters, best_text, progress_args)
                    break
    
        # randomly sample sample images and get features
        if batch_size is None:
            target_features = all_target_features
        else:
            curr_indx = torch.randperm(len(all_target_features))
            target_features = all_target_features[curr_indx][0:batch_size]
            
        universal_target_features = all_target_features
        
        # forward projection
        projected_embeds, nn_indices = nn_project(prompt_embeds, token_embedding, print_hits=False)

        # get cosine similarity score with all target features
        with torch.no_grad():
            padded_embeds = dummy_embeds.detach().clone()
            padded_embeds[dummy_ids == -1] = projected_embeds.reshape(-1, p_dim)
            logits_per_image, _ = forward_text_embedding(model, padded_embeds, dummy_ids, universal_target_features)
            scores_per_prompt = logits_per_image.mean(dim=0)
            universal_cosim_score = scores_per_prompt.max().item()
            best_indx = scores_per_prompt.argmax().item()
        
        tmp_embeds = prompt_embeds.detach().clone()
        tmp_embeds.data = projected_embeds.data
        tmp_embeds.requires_grad = True
        
        # padding
        padded_embeds = dummy_embeds.detach().clone()
        padded_embeds[dummy_ids == -1] = tmp_embeds.reshape(-1, p_dim)
        
        logits_per_image, _ = forward_text_embedding(model, padded_embeds, dummy_ids, target_features)
        cosim_scores = logits_per_image
        loss = 1 - cosim_scores.mean()
        
        prompt_embeds.grad, = torch.autograd.grad(loss, [tmp_embeds])
        
        input_optimizer.step()
        input_optimizer.zero_grad()

        curr_lr = input_optimizer.param_groups[0]["lr"]
        cosim_scores = cosim_scores.mean().item()

        decoded_text = decode_ids(nn_indices, tokenizer)[best_indx]
        if print_step is not None and (step % print_step == 0 or step == opt_iters-1):
            print(f"step: {step}, lr: {curr_lr}, cosim: {universal_cosim_score:.3f}, text: {decoded_text}")
        
        if best_sim < universal_cosim_score:
            best_sim = universal_cosim_score
            
            best_text = decoded_text

    if not on_progress is None:
        on_progress(step + 1, opt_iters, best_text, progress_args)

    if print_step is not None:
        print()
        print(f"best cosine sim: {best_sim}")
        print(f"best prompt: {best_text}")

    return best_text


def optimize_prompt(model, preprocess, device, clip_model, prompt_len, opt_iters, lr, weight_decay, prompt_bs, print_step, batch_size, target_images=None, target_prompts=None, on_progress=None, progress_steps=[1], progress_args=None):
    if not state.installed:
        raise ModuleNotFoundError("Some required packages are not installed")

    token_embedding = model.token_embedding
    tokenizer = open_clip.tokenizer._tokenizer

    # get target features
    if not target_images is None:
        all_target_features = get_target_feature_images(model, preprocess, device, target_images)
    elif not target_prompts is None:
        tokenizer_funct = open_clip.get_tokenizer(clip_model)
        all_target_features = get_target_feature_prompts(model, tokenizer_funct, device, target_prompts)
    else:
        raise ValueError("No input images or prompts")
    
    # optimize prompt
    learned_prompt = optimize_prompt_loop(
        model, tokenizer, token_embedding, all_target_features, device,
        prompt_len, opt_iters, lr, weight_decay, prompt_bs, print_step, batch_size,
        on_progress, progress_steps, progress_args
    )
    
    return learned_prompt
