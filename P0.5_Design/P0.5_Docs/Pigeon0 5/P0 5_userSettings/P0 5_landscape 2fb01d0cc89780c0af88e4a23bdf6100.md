# P0.5_landscape

- Pigeon is designed to run in either portrait or landscape.
- P0.5_landscape refers to Pigeon running in a landscape orientation
- Pigeon’s orientation can be determined by the active display’s resolution, but it also can be determined by a manual user selection
- I use the term VARIABLE axis to mean the axis that is subject to being cropped. The VARIABLE axis is always the larger of the two axis.
    - In landscape, cropping happens starting at the right side of the frame
    - in Portrait, cropping happens starting at the very bottom of the frame

- Check to see if [P0.5_userSettings](../P0%205_userSettings%202fb01d0cc89780238a22ee684f73be10.md) contains any previous selections for orientation
    1. If [P0.5_userSettings](../P0%205_userSettings%202fb01d0cc89780238a22ee684f73be10.md) shows that the current display has been used before, check to see if there are any manually selected orientation options.
        1. If manual orientation selections are found, use them
        2. If no manual orientation selections are found, determine orientation based upon the rules listed below.
            1. If x-resolution (VARIABLE axis) > y-resolution (FIXED axis); use [P0.5_landscape](P0%205_landscape%202fb01d0cc89780c0af88e4a23bdf6100.md) 
            2. If x-resolution (VARIABLE axis) == y-resolution (FIXED axis); use [P0.5_landscape](P0%205_landscape%202fb01d0cc89780c0af88e4a23bdf6100.md) 
            3. If x-resolution (VARIABLE axis)< y-resolution (FIXEX axis); use [P0.5_portrait ](P0%205_portrait%202fb01d0cc89780a4910bc66ebfc15033.md)