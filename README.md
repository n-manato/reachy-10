# reachy-10

A synchronized **Reachy Mini robot orchestra**: one lead robot and eight
instrumentalists, driven from one PC over an isolated Wi-Fi network on a shared
musical timeline. Each folder is a self-contained version of the show with its
own README, songs and tools.

| Folder | Show | What happens |
|---|---|---|
| [small-world/](small-world/) | Small World | A conductor introduces each player, then the ensemble performs together and all nine bow (about 144 seconds). |
| [mande/](mande/) | Mande ensemble | The same orchestra adapted for a West African set — kora, ngoni, tama, sangban and karignan voices playing *Lamban* (about 200 seconds). |

`mande/` started as a copy of `small-world/`; comparing the two folders shows
what it takes to re-voice the orchestra for a different repertoire.

The three-robot exhibits live in a separate repository,
[reachy-3](https://github.com/n-manato/reachy-3).

## Layout

One repository, one folder per show. Each folder is run in place: it holds its
own virtual environment, and the systemd unit under `small-world/deploy/`
points at it.

Voice-bank and vocoder models, extracted instrument samples and reference
audio are not redistributable and are not part of this repository; each
project's README explains how to obtain them.
