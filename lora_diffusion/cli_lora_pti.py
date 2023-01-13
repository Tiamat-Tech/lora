# Bootstrapped from:
# https://github.com/huggingface/diffusers/blob/main/examples/dreambooth/train_dreambooth.py

import argparse
import hashlib
import inspect
import itertools
import math
import os
import random
import re
from pathlib import Path
from typing import Optional, List, Literal

import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.checkpoint
from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    StableDiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers.optimization import get_scheduler
from huggingface_hub import HfFolder, Repository, whoami
from PIL import Image
from torch.utils.data import Dataset
from torchvision import transforms
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer
import wandb
import fire

from lora_diffusion import (
    PivotalTuningDatasetCapation,
    extract_lora_ups_down,
    inject_trainable_lora,
    inject_trainable_lora_extended,
    inspect_lora,
    save_lora_weight,
    save_all,
    prepare_clip_model_sets,
    evaluate_pipe,
    UNET_EXTENDED_TARGET_REPLACE,
)


def get_models(
    pretrained_model_name_or_path,
    pretrained_vae_name_or_path,
    revision,
    placeholder_tokens: List[str],
    initializer_tokens: List[str],
    device="cuda:0",
):

    tokenizer = CLIPTokenizer.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="tokenizer",
        revision=revision,
    )

    text_encoder = CLIPTextModel.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="text_encoder",
        revision=revision,
    )

    placeholder_token_ids = []

    for token, init_tok in zip(placeholder_tokens, initializer_tokens):
        num_added_tokens = tokenizer.add_tokens(token)
        if num_added_tokens == 0:
            raise ValueError(
                f"The tokenizer already contains the token {token}. Please pass a different"
                " `placeholder_token` that is not already in the tokenizer."
            )

        placeholder_token_id = tokenizer.convert_tokens_to_ids(token)

        placeholder_token_ids.append(placeholder_token_id)

        # Load models and create wrapper for stable diffusion

        text_encoder.resize_token_embeddings(len(tokenizer))
        token_embeds = text_encoder.get_input_embeddings().weight.data
        if init_tok.startswith("<rand"):
            # <rand-"sigma">, e.g. <rand-0.5>
            sigma_val = float(re.findall(r"<rand-(.*)>", init_tok)[0])

            token_embeds[placeholder_token_id] = (
                torch.randn_like(token_embeds[0]) * sigma_val
            )
            print(
                f"Initialized {token} with random noise (sigma={sigma_val}), empirically {token_embeds[placeholder_token_id].mean().item():.3f} +- {token_embeds[placeholder_token_id].std().item():.3f}"
            )
            print(f"Norm : {token_embeds[placeholder_token_id].norm():.4f}")

        elif init_tok == "<zero>":
            token_embeds[placeholder_token_id] = torch.zeros_like(token_embeds[0])
        else:
            token_ids = tokenizer.encode(init_tok, add_special_tokens=False)
            # Check if initializer_token is a single token or a sequence of tokens
            if len(token_ids) > 1:
                raise ValueError("The initializer token must be a single token.")

            initializer_token_id = token_ids[0]
            token_embeds[placeholder_token_id] = token_embeds[initializer_token_id]

    vae = AutoencoderKL.from_pretrained(
        pretrained_vae_name_or_path or pretrained_model_name_or_path,
        subfolder=None if pretrained_vae_name_or_path else "vae",
        revision=None if pretrained_vae_name_or_path else revision,
    )
    unet = UNet2DConditionModel.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="unet",
        revision=revision,
    )

    return (
        text_encoder.to(device),
        vae.to(device),
        unet.to(device),
        tokenizer,
        placeholder_token_ids,
    )


def text2img_dataloader(train_dataset, train_batch_size, tokenizer, vae, text_encoder):
    def collate_fn(examples):
        input_ids = [example["instance_prompt_ids"] for example in examples]
        pixel_values = [example["instance_images"] for example in examples]

        # Concat class and instance examples for prior preservation.
        # We do this to avoid doing two forward passes.
        if examples[0].get("class_prompt_ids", None) is not None:
            input_ids += [example["class_prompt_ids"] for example in examples]
            pixel_values += [example["class_images"] for example in examples]

        pixel_values = torch.stack(pixel_values)
        pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()

        input_ids = tokenizer.pad(
            {"input_ids": input_ids},
            padding="max_length",
            max_length=tokenizer.model_max_length,
            return_tensors="pt",
        ).input_ids

        batch = {
            "input_ids": input_ids,
            "pixel_values": pixel_values,
        }

        if examples[0].get("mask", None) is not None:
            batch["mask"] = torch.stack([example["mask"] for example in examples])

        return batch

    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=train_batch_size,
        shuffle=True,
        collate_fn=collate_fn,
    )

    return train_dataloader


def loss_step(
    batch,
    unet,
    vae,
    text_encoder,
    scheduler,
    t_mutliplier=1.0,
    mixed_precision=False,
):
    weight_dtype = torch.float32

    latents = vae.encode(
        batch["pixel_values"].to(dtype=weight_dtype).to(unet.device)
    ).latent_dist.sample()
    latents = latents * 0.18215

    noise = torch.randn_like(latents)
    bsz = latents.shape[0]

    timesteps = torch.randint(
        0,
        int(scheduler.config.num_train_timesteps * t_mutliplier),
        (bsz,),
        device=latents.device,
    )
    timesteps = timesteps.long()

    noisy_latents = scheduler.add_noise(latents, noise, timesteps)

    if mixed_precision:
        with torch.cuda.amp.autocast():

            encoder_hidden_states = text_encoder(
                batch["input_ids"].to(text_encoder.device)
            )[0]

            model_pred = unet(noisy_latents, timesteps, encoder_hidden_states).sample
    else:

        encoder_hidden_states = text_encoder(
            batch["input_ids"].to(text_encoder.device)
        )[0]

        model_pred = unet(noisy_latents, timesteps, encoder_hidden_states).sample

    if scheduler.config.prediction_type == "epsilon":
        target = noise
    elif scheduler.config.prediction_type == "v_prediction":
        target = scheduler.get_velocity(latents, noise, timesteps)
    else:
        raise ValueError(f"Unknown prediction type {scheduler.config.prediction_type}")

    if batch.get("mask", None) is not None:

        mask = (
            batch["mask"]
            .to(model_pred.device)
            .reshape(
                model_pred.shape[0], 1, model_pred.shape[2] * 8, model_pred.shape[3] * 8
            )
        )
        # resize to match model_pred
        mask = (
            F.interpolate(
                mask.float(),
                size=model_pred.shape[-2:],
                mode="nearest",
            )
            + 0.05
        )

        mask = mask / mask.mean()

        model_pred = model_pred * mask

        target = target * mask

    loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
    return loss


def train_inversion(
    unet,
    vae,
    text_encoder,
    dataloader,
    num_steps: int,
    scheduler,
    index_no_updates,
    optimizer,
    save_steps: int,
    placeholder_token_ids,
    placeholder_tokens,
    save_path: str,
    tokenizer,
    lr_scheduler,
    test_image_path: str,
    accum_iter: int = 1,
    log_wandb: bool = False,
    wandb_log_prompt_cnt: int = 10,
    class_token: str = "person",
    mixed_precision: bool = False,
    clip_ti_decay: bool = True,
):

    progress_bar = tqdm(range(num_steps))
    progress_bar.set_description("Steps")
    global_step = 0

    # Original Emb for TI
    orig_embeds_params = text_encoder.get_input_embeddings().weight.data.clone()

    if log_wandb:
        preped_clip = prepare_clip_model_sets()

    index_updates = ~index_no_updates
    loss_sum = 0.0

    for epoch in range(math.ceil(num_steps / len(dataloader))):
        unet.eval()
        text_encoder.train()
        for batch in dataloader:

            lr_scheduler.step()

            with torch.set_grad_enabled(True):
                loss = (
                    loss_step(
                        batch,
                        unet,
                        vae,
                        text_encoder,
                        scheduler,
                        mixed_precision=mixed_precision,
                    )
                    / accum_iter
                )

                loss.backward()
                loss_sum += loss.detach().item()

                if global_step % accum_iter == 0:
                    optimizer.step()
                    optimizer.zero_grad()

                    with torch.no_grad():

                        # normalize embeddings
                        if clip_ti_decay:
                            pre_norm = (
                                text_encoder.get_input_embeddings()
                                .weight[index_updates, :]
                                .norm(dim=-1, keepdim=True)
                            )

                            lambda_ = min(1.0, 100 * lr_scheduler.get_last_lr()[0])
                            text_encoder.get_input_embeddings().weight[
                                index_updates
                            ] = F.normalize(
                                text_encoder.get_input_embeddings().weight[
                                    index_updates, :
                                ],
                                dim=-1,
                            ) * (
                                pre_norm + lambda_ * (0.4 - pre_norm)
                            )
                            print(pre_norm)

                        current_norm = (
                            text_encoder.get_input_embeddings()
                            .weight[index_updates, :]
                            .norm(dim=-1)
                        )

                        text_encoder.get_input_embeddings().weight[
                            index_no_updates
                        ] = orig_embeds_params[index_no_updates]

                        print(f"Current Norm : {current_norm}")

                global_step += 1
                progress_bar.update(1)

                logs = {
                    "loss": loss.detach().item(),
                    "lr": lr_scheduler.get_last_lr()[0],
                }
                progress_bar.set_postfix(**logs)

            if global_step % save_steps == 0:
                save_all(
                    unet=unet,
                    text_encoder=text_encoder,
                    placeholder_token_ids=placeholder_token_ids,
                    placeholder_tokens=placeholder_tokens,
                    save_path=os.path.join(
                        save_path, f"step_inv_{global_step}.safetensors"
                    ),
                    save_lora=False,
                )
                if log_wandb:
                    with torch.no_grad():
                        pipe = StableDiffusionPipeline(
                            vae=vae,
                            text_encoder=text_encoder,
                            tokenizer=tokenizer,
                            unet=unet,
                            scheduler=scheduler,
                            safety_checker=None,
                            feature_extractor=None,
                        )

                        # open all images in test_image_path
                        images = []
                        for file in os.listdir(test_image_path):
                            if file.endswith(".png") or file.endswith(".jpg"):
                                images.append(
                                    Image.open(os.path.join(test_image_path, file))
                                )

                        wandb.log({"loss": loss_sum / save_steps})
                        loss_sum = 0.0
                        wandb.log(
                            evaluate_pipe(
                                pipe,
                                target_images=images,
                                class_token=class_token,
                                learnt_token="".join(placeholder_tokens),
                                n_test=wandb_log_prompt_cnt,
                                n_step=50,
                                clip_model_sets=preped_clip,
                            )
                        )

            if global_step >= num_steps:
                return


def perform_tuning(
    unet,
    vae,
    text_encoder,
    dataloader,
    num_steps,
    scheduler,
    optimizer,
    save_steps: int,
    placeholder_token_ids,
    placeholder_tokens,
    save_path,
    lr_scheduler_lora,
    lora_unet_target_modules,
    lora_clip_target_modules,
):

    progress_bar = tqdm(range(num_steps))
    progress_bar.set_description("Steps")
    global_step = 0

    weight_dtype = torch.float16

    unet.train()
    text_encoder.train()

    for epoch in range(math.ceil(num_steps / len(dataloader))):
        for batch in dataloader:
            lr_scheduler_lora.step()

            optimizer.zero_grad()

            loss = loss_step(
                batch,
                unet,
                vae,
                text_encoder,
                scheduler,
                t_mutliplier=0.8,
                mixed_precision=True,
            )
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                itertools.chain(unet.parameters(), text_encoder.parameters()), 1.0
            )
            optimizer.step()
            progress_bar.update(1)
            logs = {
                "loss": loss.detach().item(),
                "lr": lr_scheduler_lora.get_last_lr()[0],
            }
            progress_bar.set_postfix(**logs)

            global_step += 1

            if global_step % save_steps == 0:
                save_all(
                    unet,
                    text_encoder,
                    placeholder_token_ids=placeholder_token_ids,
                    placeholder_tokens=placeholder_tokens,
                    save_path=os.path.join(
                        save_path, f"step_{global_step}.safetensors"
                    ),
                    target_replace_module_text=lora_clip_target_modules,
                    target_replace_module_unet=lora_unet_target_modules,
                )
                moved = (
                    torch.tensor(list(itertools.chain(*inspect_lora(unet).values())))
                    .mean()
                    .item()
                )

                print("LORA Unet Moved", moved)
                moved = (
                    torch.tensor(
                        list(itertools.chain(*inspect_lora(text_encoder).values()))
                    )
                    .mean()
                    .item()
                )

                print("LORA CLIP Moved", moved)

            if global_step >= num_steps:
                return


def train(
    instance_data_dir: str,
    pretrained_model_name_or_path: str,
    output_dir: str,
    train_text_encoder: bool = False,
    pretrained_vae_name_or_path: str = None,
    revision: Optional[str] = None,
    class_data_dir: Optional[str] = None,
    stochastic_attribute: Optional[str] = None,
    perform_inversion: bool = True,
    use_template: Literal[None, "object", "style"] = None,
    placeholder_tokens: str = "<s>",
    placeholder_token_at_data: Optional[str] = None,
    initializer_tokens: Optional[str] = None,
    class_prompt: Optional[str] = None,
    with_prior_preservation: bool = False,
    prior_loss_weight: float = 1.0,
    num_class_images: int = 100,
    seed: int = 42,
    resolution: int = 512,
    color_jitter: bool = True,
    train_batch_size: int = 1,
    sample_batch_size: int = 1,
    max_train_steps_tuning: int = 1000,
    max_train_steps_ti: int = 1000,
    save_steps: int = 100,
    gradient_accumulation_steps: int = 4,
    gradient_checkpointing: bool = False,
    mixed_precision="fp16",
    lora_rank: int = 4,
    lora_unet_target_modules={"CrossAttention", "Attention", "GEGLU"},
    lora_clip_target_modules={"CLIPAttention"},
    use_extended_lora: bool = False,
    clip_ti_decay: bool = True,
    learning_rate_unet: float = 1e-4,
    learning_rate_text: float = 1e-5,
    learning_rate_ti: float = 5e-4,
    continue_inversion: bool = True,
    continue_inversion_lr: Optional[float] = None,
    use_face_segmentation_condition: bool = False,
    scale_lr: bool = False,
    lr_scheduler: str = "linear",
    lr_warmup_steps: int = 0,
    lr_scheduler_lora: str = "linear",
    lr_warmup_steps_lora: int = 0,
    weight_decay_ti: float = 0.00,
    weight_decay_lora: float = 0.001,
    use_8bit_adam: bool = False,
    device="cuda:0",
    extra_args: Optional[dict] = None,
    log_wandb: bool = False,
    wandb_log_prompt_cnt: int = 10,
    wandb_project_name: str = "new_pti_project",
    wandb_entity: str = "new_pti_entity",
):
    torch.manual_seed(seed)

    if log_wandb:
        wandb.init(
            project=wandb_project_name,
            entity=wandb_entity,
            name=f"steps_{max_train_steps_ti}_lr_{learning_rate_ti}_{instance_data_dir.split('/')[-1]}",
            reinit=True,
            config={
                "lr": learning_rate_ti,
                **(extra_args if extra_args is not None else {}),
            },
        )

    if output_dir is not None:
        os.makedirs(output_dir, exist_ok=True)
    # print(placeholder_tokens, initializer_tokens)
    placeholder_tokens = placeholder_tokens.split("|")
    if initializer_tokens is None:
        print("PTI : Initializer Token not give, random inits")
        initializer_tokens = ["<rand-0.017>"] * len(placeholder_tokens)
    else:
        initializer_tokens = initializer_tokens.split("|")

    assert len(initializer_tokens) == len(
        placeholder_tokens
    ), "Unequal Initializer token for Placeholder tokens."

    class_token = "".join(initializer_tokens)

    if placeholder_token_at_data is not None:
        tok, pat = placeholder_token_at_data.split("|")
        token_map = {tok: pat}

    else:
        token_map = {"DUMMY": "".join(placeholder_tokens)}

    print("Placeholder Tokens", placeholder_tokens)
    print("Initializer Tokens", initializer_tokens)

    # get the models
    text_encoder, vae, unet, tokenizer, placeholder_token_ids = get_models(
        pretrained_model_name_or_path,
        pretrained_vae_name_or_path,
        revision,
        placeholder_tokens,
        initializer_tokens,
        device=device,
    )

    noise_scheduler = DDPMScheduler.from_config(
        pretrained_model_name_or_path, subfolder="scheduler"
    )

    if gradient_checkpointing:
        unet.enable_gradient_checkpointing()

    if scale_lr:
        unet_lr = learning_rate_unet * gradient_accumulation_steps * train_batch_size
        text_encoder_lr = (
            learning_rate_text * gradient_accumulation_steps * train_batch_size
        )
        ti_lr = learning_rate_ti * gradient_accumulation_steps * train_batch_size
    else:
        unet_lr = learning_rate_unet
        text_encoder_lr = learning_rate_text
        ti_lr = learning_rate_ti

    train_dataset = PivotalTuningDatasetCapation(
        instance_data_root=instance_data_dir,
        stochastic_attribute=stochastic_attribute,
        token_map=token_map,
        use_template=use_template,
        class_data_root=class_data_dir if with_prior_preservation else None,
        class_prompt=class_prompt,
        tokenizer=tokenizer,
        size=resolution,
        color_jitter=color_jitter,
        use_face_segmentation_condition=use_face_segmentation_condition,
    )

    train_dataset.blur_amount = 200

    train_dataloader = text2img_dataloader(
        train_dataset, train_batch_size, tokenizer, vae, text_encoder
    )

    index_no_updates = torch.arange(len(tokenizer)) != placeholder_token_ids[0]

    for tok_id in placeholder_token_ids:
        index_no_updates[tok_id] = False

    unet.requires_grad_(False)
    vae.requires_grad_(False)

    params_to_freeze = itertools.chain(
        text_encoder.text_model.encoder.parameters(),
        text_encoder.text_model.final_layer_norm.parameters(),
        text_encoder.text_model.embeddings.position_embedding.parameters(),
    )
    for param in params_to_freeze:
        param.requires_grad = False

    # STEP 1 : Perform Inversion
    if perform_inversion:
        ti_optimizer = optim.AdamW(
            text_encoder.get_input_embeddings().parameters(),
            lr=ti_lr,
            betas=(0.9, 0.999),
            eps=1e-08,
            weight_decay=weight_decay_ti,
        )

        lr_scheduler = get_scheduler(
            lr_scheduler,
            optimizer=ti_optimizer,
            num_warmup_steps=lr_warmup_steps,
            num_training_steps=max_train_steps_ti,
        )

        train_inversion(
            unet,
            vae,
            text_encoder,
            train_dataloader,
            max_train_steps_ti,
            accum_iter=gradient_accumulation_steps,
            scheduler=noise_scheduler,
            index_no_updates=index_no_updates,
            optimizer=ti_optimizer,
            lr_scheduler=lr_scheduler,
            save_steps=save_steps,
            placeholder_tokens=placeholder_tokens,
            placeholder_token_ids=placeholder_token_ids,
            save_path=output_dir,
            test_image_path=instance_data_dir,
            log_wandb=log_wandb,
            wandb_log_prompt_cnt=wandb_log_prompt_cnt,
            class_token=class_token,
            mixed_precision=False,
            tokenizer=tokenizer,
            clip_ti_decay=clip_ti_decay,
        )

        del ti_optimizer

    # Next perform Tuning with LoRA:
    if not use_extended_lora:
        unet_lora_params, _ = inject_trainable_lora(
            unet, r=lora_rank, target_replace_module=lora_unet_target_modules
        )
    else:
        print("USING EXTENDED UNET!!!")
        lora_unet_target_modules = (
            lora_unet_target_modules | UNET_EXTENDED_TARGET_REPLACE
        )
        print("Will replace modules: ", lora_unet_target_modules)

        unet_lora_params, _ = inject_trainable_lora_extended(
            unet, r=lora_rank, target_replace_module=lora_unet_target_modules
        )
    print(f"PTI : has {len(unet_lora_params)} lora")

    print("Before training:")
    inspect_lora(unet)

    params_to_optimize = [
        {"params": itertools.chain(*unet_lora_params), "lr": unet_lr},
    ]

    text_encoder.requires_grad_(False)

    if continue_inversion:
        params_to_optimize += [
            {
                "params": text_encoder.get_input_embeddings().parameters(),
                "lr": continue_inversion_lr
                if continue_inversion_lr is not None
                else ti_lr,
            }
        ]
        text_encoder.requires_grad_(True)
        params_to_freeze = itertools.chain(
            text_encoder.text_model.encoder.parameters(),
            text_encoder.text_model.final_layer_norm.parameters(),
            text_encoder.text_model.embeddings.position_embedding.parameters(),
        )
        for param in params_to_freeze:
            param.requires_grad = False

    if train_text_encoder:
        text_encoder_lora_params, _ = inject_trainable_lora(
            text_encoder,
            target_replace_module=lora_clip_target_modules,
            r=lora_rank,
        )
        params_to_optimize += [
            {
                "params": itertools.chain(*text_encoder_lora_params),
                "lr": text_encoder_lr,
            }
        ]
        inspect_lora(text_encoder)

    lora_optimizers = optim.AdamW(params_to_optimize, weight_decay=weight_decay_lora)

    unet.train()
    if train_text_encoder:
        text_encoder.train()

    train_dataset.blur_amount = 70

    lr_scheduler_lora = get_scheduler(
        lr_scheduler_lora,
        optimizer=lora_optimizers,
        num_warmup_steps=lr_warmup_steps_lora,
        num_training_steps=max_train_steps_tuning,
    )

    perform_tuning(
        unet,
        vae,
        text_encoder,
        train_dataloader,
        max_train_steps_tuning,
        scheduler=noise_scheduler,
        optimizer=lora_optimizers,
        save_steps=save_steps,
        placeholder_tokens=placeholder_tokens,
        placeholder_token_ids=placeholder_token_ids,
        save_path=output_dir,
        lr_scheduler_lora=lr_scheduler_lora,
        lora_unet_target_modules=lora_unet_target_modules,
        lora_clip_target_modules=lora_clip_target_modules,
    )


def main():
    fire.Fire(train)
