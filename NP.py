import torch
import torch.utils.data
from torch import nn, optim
from torch.nn import functional as F
from torchvision import datasets, transforms

import time
from argparse import ArgumentParser
from models import *
from utils import *
from tensorboardX import SummaryWriter
from complete_image import get_sample_images, random_mask


def train(context_encoder, context_to_dist, decoder, aggregator, train_loader, test_loader, optimizer, n_epochs, device,
          save_path,
          summary_writer, save_every=10, h=28, w=28, log=1):
    context_encoder.train()
    decoder.train()
    grid = make_mesh_grid(h, w).to(device).view(h * w, 2)  # size 784*2
    for epoch in range(n_epochs):
        running_loss = 0.0
        last_log_time = time.time()

        # Training
        train_loss = 0.0
        for batch_idx, (batch, _) in enumerate(train_loader):
            batch = batch.to(device)
            if ((batch_idx % 100) == 0) and batch_idx > 1:
                print("epoch {} | batch {} | mean running loss {:.2f} | {:.2f} batch/s".format(epoch, batch_idx,
                                                                                               running_loss / 100,
                                                                                               100 / (
                                                                                                       time.time() - last_log_time)))
                last_log_time = time.time()
                running_loss = 0.0

            mask = random_mask_uniform(batch_shape=(batch.size(0), h * w), device=batch.device, h=h, w=w)
            context_data = torch.cat(
                [batch.view(batch.size(0), h * w, 1), grid.unsqueeze(0).expand(batch.size(0), h * w, 2)], dim=-1)

            # context data size (bsize,h*w,3) with 3 = (pixel value, coord_x,coord_y)

            context_full = context_encoder(context_data)  # size bsize,h*w,d with d =hidden size

            mask = mask.unsqueeze(-1)
            r_masked = aggregator.forward(context_full, mask=mask, agg_dim=1)  # bsize * hidden_size
            r_full = aggregator.forward(context_full, mask=None, agg_dim=1)
            # print("relative diff between masked and full {:.2f}".format(torch.norm(r_masked-r_full)/torch.norm(r_full)))

            ## compute loss
            z_params_full = context_to_dist(r_full)
            z_params_masked = context_to_dist(r_masked)
            z_full = sample_z(z_params_full)  # size bsize * hidden
            z_full = z_full.unsqueeze(1).expand(-1, h * w, -1)

            # resize context to have one context per input coordinate
            grid_input = grid.unsqueeze(0).expand(batch.size(0), -1, -1)
            target_input = torch.cat([z_full, grid_input], dim=-1)

            reconstructed_image = decoder.forward(target_input)
            if batch_idx == 0 and log:
                if not os.path.exists("images"):
                    os.makedirs("images")
                save_images_batch(batch.cpu(), "images/target_epoch_{}".format(epoch))
                save_images_batch(reconstructed_image.cpu(), "images/reconstruct_epoch_{}".format(epoch))
            reconstruction_loss = (F.binary_cross_entropy(reconstructed_image, batch.view(batch.size(0), h * w, 1),
                                                          reduction='none') * (1 - mask)).sum(dim=1).mean()

            kl_loss = kl_normal(z_params_full, z_params_masked).mean()
            if batch_idx % 100 == 0:
                psnr = metrics.psnr(reconstructed_image, batch)
                cos_sim = metrics.cosine_similarity(reconstructed_image, batch)
                ssim = metrics.ssim(reconstructed_image, batch)
                print("reconstruction {:.2f} | kl {:.2f} | PSNR {:.2f}dB | Cosine similarity: {:.2f} | SSIM: {:.2f}".format(reconstruction_loss, kl_loss, psnr, cos_sim, ssim))

            loss = reconstruction_loss + kl_loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # add loss
            running_loss += loss.item()
            train_loss += loss.item()

        print("Epoch train loss : {}".format(train_loss / len(train_loader)))
        if summary_writer is not None:
            summary_writer.add_scalar("train/loss", train_loss / len(train_loader), global_step=epoch)
        if (epoch % save_every == 0) and log and epoch > 0:
            save_model(save_path, "NP_model_epoch_{}.pt".format(epoch), context_encoder, context_to_dist,
                       decoder,
                       device)

        test_loss = 0.0
        with torch.no_grad():
            for batch_idx, (batch, _) in enumerate(test_loader):
                batch = batch.to(device)

                mask = random_mask_uniform(batch_shape=(batch.size(0), h * w), device=batch.device, h=h, w=w)

                context_data = torch.cat(
                    [batch.view(batch.size(0), h * w, 1), grid.unsqueeze(0).expand(batch.size(0), h * w, 2)], dim=-1)
                # context data size (bsize,h*w,3) with 3 = (pixel value, coord_x,coord_y)

                context_full = context_encoder(context_data)  # size bsize,h*w,d with d =hidden size

                mask = mask.unsqueeze(-1)  # size bsize * 784 *
                r_masked = aggregator.forward(context_full, mask=mask, agg_dim=1)  # bsize * hidden_size
                r_full = aggregator.forward(context_full, mask=None, agg_dim=1)
                # print("relative diff between masked and full {:.2f}".format(torch.norm(r_masked-r_full)/torch.norm(r_full)))

                ## compute loss
                z_params_full = context_to_dist(r_full)
                z_params_masked = context_to_dist(r_masked)
                z_full = sample_z(z_params_full)  # size bsize * hidden
                z_full = z_full.unsqueeze(1).expand(-1, h * w, -1)

                # resize context to have one context per input coordinate
                grid_input = grid.unsqueeze(0).expand(batch.size(0), -1, -1)
                target_input = torch.cat([z_full, grid_input], dim=-1)

                reconstructed_image = decoder.forward(target_input)
                reconstruction_loss = (F.binary_cross_entropy(reconstructed_image, batch.view(batch.size(0), h * w, 1),
                                                              reduction='none') * (1 - mask)).sum(dim=1).mean()

                kl_loss = kl_normal(z_params_full, z_params_masked).mean()
                loss = reconstruction_loss + kl_loss
                test_loss += loss.item()
            if summary_writer is not None:
                summary_writer.add_scalar("test/loss", test_loss / len(test_loader), global_step=epoch)

            # do examples

            test_batch, _ = next(iter(test_loader))
            test_batch = test_batch[:10]
            test_batch = test_batch.view(test_batch.size(0), -1, 1).to(device)  # bsize * 784 *1
            for n_pixels in [50, 150, 450]:
                mask = random_mask(test_batch.size(0), n_pixels, total_pixels=784)

            image = get_sample_images(test_batch, h, w, context_encoder, context_to_dist, decoder, n_pixels, 4,
                                      mask=mask,
                                      save=False)
            image = torch.tensor(image).transpose(0, 2).unsqueeze(0).transpose(2, 3)

            # import pdb;
            # pdb.set_trace()
            if summary_writer is not None:
                summary_writer.add_image("test_image/{}_pixels".format(n_pixels), image, global_step=epoch)

    return


def main(args):
    if not os.path.isdir(args.log_dir):
        os.makedirs(args.log_dir)

    summary_writer = SummaryWriter(log_dir=args.log_dir) if args.log else None

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    train_loader = torch.utils.data.DataLoader(

        datasets.MNIST('../data', train=True, download=True,
                       transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Lambda(lambda x: (x > .5).float())
                       ])),
        batch_size=args.bsize, shuffle=True)

    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST('../data', train=False, transform=transforms.Compose([
            transforms.ToTensor(),
            transforms.Lambda(lambda x: (x > .5).float())
        ])),
        batch_size=args.bsize, shuffle=True)

    context_encoder = ContextEncoder()
    context_to_dist = ContextToLatentDistribution()
    decoder = Decoder()

    if args.resume_file is not None:
        load_models(args.resume_file, context_encoder, context_to_dist, decoder)
    context_encoder = model = context_encoder.to(device)
    decoder = decoder.to(device)
    context_to_dist = context_to_dist.to(device)
    full_model_params = list(context_encoder.parameters()) + list(decoder.parameters()) + list(
        context_to_dist.parameters())
    optimizer = optim.Adam(full_model_params, lr=args.lr)

    if args.aggregator == "mean":
        aggregator = MeanAgregator()
    else:
        assert args.aggregator == "attention"
        aggregator = AttentionAggregator(128)

    train(context_encoder, context_to_dist, decoder, aggregator, train_loader, test_loader, optimizer, args.epochs,
          device,
          args.models_path, summary_writer=summary_writer, save_every=args.save_every, log=args.log)


parser = ArgumentParser()
parser.add_argument("--models_path", type=str, default="models/")
parser.add_argument("--save_model", type=int, default=1)
parser.add_argument("--lr", type=float, default=1e-3)
parser.add_argument("--epochs", type=int, default=100)
parser.add_argument("--bsize", type=int, default=32)
parser.add_argument("--resume_file", type=str, default=None)
parser.add_argument("--save_every", type=int, default=10)
parser.add_argument("--log_dir", type=str, default="logs")
parser.add_argument("--aggregator", type=str, choices=['mean', 'attention'], default='mean')
parser.add_argument("--log", type=int, default=1)
if __name__ == '__main__':
    args = parser.parse_args()
    main(args)
