import json
import numpy as np
import matplotlib.pyplot as plt

def load_losses(file_path):
    with open(file_path, 'r') as f:
        losses = json.load(f)
    return losses

def plot_loaded_losses():
    step_interval = 1  # Adjust this value if needed

    contrast_losses = np.array(load_losses('contrast_losses.json')['contrast_losses'])
    classification_losses = np.array(load_losses('classification_losses.json')['classification_losses'])
    total_losses = np.array(load_losses('total_losses.json')['total_losses'])
    reconstruction_losses = np.array(load_losses('reconstruction_losses.json')['reconstruction_losses'])

    fig, ax = plt.subplots(2, 2, figsize=(15, 10))

    points = len(contrast_losses)
    steps = np.arange(0, points * step_interval, step_interval)
    ax[0, 0].plot(steps, contrast_losses, label='Contrastive Loss')
    ax[0, 0].set_title('Contrastive Loss')
    ax[0, 0].set_xlabel('Steps')
    ax[0, 0].set_ylabel('Loss')
    ax[0, 0].legend()

    points = len(classification_losses)
    steps = np.arange(0, points * step_interval, step_interval)
    ax[0, 1].plot(steps, classification_losses, label='Classification Loss')
    ax[0, 1].set_title('Classification Loss')
    ax[0, 1].set_xlabel('Steps')
    ax[0, 1].set_ylabel('Loss')
    ax[0, 1].legend()

    points = len(reconstruction_losses)
    steps = np.arange(0, points * step_interval, step_interval)
    ax[1, 0].plot(steps, reconstruction_losses, label='Reconstruction Loss')
    ax[1, 0].set_title('Reconstruction Loss')
    ax[1, 0].set_xlabel('Steps')
    ax[1, 0].set_ylabel('Loss')
    ax[1, 0].legend()

    points = len(total_losses)
    steps = np.arange(0, points * step_interval, step_interval)
    ax[1, 1].plot(steps, total_losses, label='Total Loss')
    ax[1, 1].set_title('Total Loss')
    ax[1, 1].set_xlabel('Steps')
    ax[1, 1].set_ylabel('Loss')
    ax[1, 1].legend()

    plt.tight_layout()
    plt.savefig('loaded_losses_plot.png')
    plt.show()

plot_loaded_losses()
