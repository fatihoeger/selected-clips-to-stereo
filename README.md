# Selected Clips to Stereo

A DaVinci Resolve script that turns multichannel audio recordings (for example polyphonic WAV files from a field recorder) into stereo files you can edit with straight away.

For every recording selected in the Media Pool, it:

- averages the recorded channels into one mix, identical on left and right
- leaves out silent channels (disarmed or unused tracks)
- fixes channels that cancel each other out, such as a boom and a lav wired in opposite polarity
- raises the mix to the peak level of the loudest source channel (never clips, never lowers the level)
- keeps the recorder's timecode and metadata
- imports the results into a new "Stereo" bin inside the current bin

Your original files are never changed.

## Download

Get the latest version from the [Releases](../../releases) page.

## Installation

- English: [How to install (English).txt](How%20to%20install%20(English).txt)
- Deutsch: [Installationsanleitung (Deutsch).txt](Installationsanleitung%20(Deutsch).txt)

In short: install [FFmpeg](https://ffmpeg.org), copy `Selected_Clips_to_Stereo_v1.py` into Resolve's `Fusion/Scripts/Edit` folder, restart Resolve, select your recordings and run **Workspace → Scripts → Selected_Clips_to_Stereo_v1**.

Requires DaVinci Resolve 21.1 or later. Tested on macOS; Windows is supported but not yet tested.

## Official version check

Each run checks that the script is the official, unmodified version and prints the result in Resolve's console (**Workspace → Console**). Copies that have been changed report **MODIFIED**.

## Terms

Copyright © 2026 Fatih Öger. All rights reserved.
Free to use. Do not modify or redistribute this script; get copies only from the author.

Questions, problems or ideas: [fatihoger@colorwise.at](mailto:fatihoger@colorwise.at)
