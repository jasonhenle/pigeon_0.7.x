# P0.5_pigeonCanvasScale

- Pigeon’s native resolution is:
    - 5126 x 2160 (2.39:1) for [P0.5_landscape](P0%205_userSettings/P0%205_landscape%202fb01d0cc89780c0af88e4a23bdf6100.md)
    - 2160 x 5126 (1:2.39) for [P0.5_portrait ](P0%205_userSettings/P0%205_portrait%202fb01d0cc89780a4910bc66ebfc15033.md)
- Example for converting from 5126 x 2160 (2.39:1) down to 1.78:1 at 1920 x 1080:
    - Reduce the number of horizontal boxes (see [P0.5_pigeonCanvas](P0%205_pigeonCanvas%202fb01d0cc89780ea95f5ee39bdc4e59f.md)) from 1-19 down to 1-14
    - Center the remaining 14 boxes along the VARIABLE axis
    - Scale [P0.5_pigeonCanvas](P0%205_pigeonCanvas%202fb01d0cc89780ea95f5ee39bdc4e59f.md) from a 3840 x 2160, down to 1920x 1080