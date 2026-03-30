# P0.5_pigeonCanvasCrop

Pigeon is intended to be flexibly adapt its UI to accommodating displays of different resolutions and orientations. Before explaining how to adapt the UI, let me explain how the native resolution is set up. The UI will often be referred to as the canvas. The native resolution of the Pigeon canvas is 5126 x 2160.

- [P0.5_pigeonCanvas](P0%205_pigeonCanvas%202fb01d0cc89780ea95f5ee39bdc4e59f.md)  info
    - Native resolution is 5126 x 2160
    - Pigeon is all about selecting widgets and arranging them yourself onto the Pigeon canvas. To help better describe placement of those widgets, and to ensure their proper alignment, the canvas has been divided into 152 boxes. Each box is 270 x 270 pixels.
        - [P0.5_landscape](P0%205_userSettings/P0%205_landscape%202fb01d0cc89780c0af88e4a23bdf6100.md)
            - 8 boxes tall FIXED [always 8 boxes tall]
            - 19 boxes wide VARIABLE [crop the x - axis, as needed]
        - [P0.5_portrait ](P0%205_userSettings/P0%205_portrait%202fb01d0cc89780a4910bc66ebfc15033.md)
            - 8 boxes wide FIXED [always 8 boxes wide]
            - 19 boxes tall VARIABLE [crop the y - axis, as needed]
    - To describe the location of each 270 x 270 box, follow this naming convention:
        - When describing the location of each box, list the FIXED part first (this is always the axis with less resolution.
            - 8 boxes can be described as A, B, C, D, E, F, G, H
        - The VARIABLE axis should be described using a number. 1 - 19

- Example for a 2.39:1 display
    - The top left box would be called A1
    - The top right box would be called A19
    - The bottom left box would be H1
    - The bottom right box would be H19
- Example for a 1.78:1 display
    - Because 1.78:1 is not as wide as 2.39:1, the VARIABLE axis will crop off what doesn’t fit.
    - With a 1.78:1 aspect ratio, the VARIABLE axis drops from 1 - 19, down to 1-14.
    - When adjusting from 2.39:1 down to 1.78:1 the VARIABLE axis does not cleanly divide into 270.
        - 4 boxes are 100% cropped off
        - 1 box is cropped off by ~80%
            - Any incomplete boxes will be excluded from use.
            - Excluding partial boxes results in wasted space along the VARIABLE axis.
            - If the VARIABLE axis is left with a gap, take the remaining boxes and center them along the VARIABLE axis. The result should be a small empty space at either end of the VARIABLE axis.
                - For a 1.78:1 aspect ratio, there will be about 30 pixels of empty space on either end of the VARIABLE axis
                - In this case, instead of the VARIABLE axis being between 1 - 19, the 1.78:1 version will only go to 1 - 14.