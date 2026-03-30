# P0.5_detectDisplay

- Detect a currently active display
    - If there are more than one active display, use the primary one.
- Detect resolution of the active display
    - Save the resolution of the current active display in [P0.5_userSettings](P0%205_userSettings%202fb01d0cc89780238a22ee684f73be10.md)
    - Convert the resolution of the active display into an aspect ratio. “[units wide] x 1”
- Determine app orientation
    - If the resolution > vertical resolution, the orientation should be