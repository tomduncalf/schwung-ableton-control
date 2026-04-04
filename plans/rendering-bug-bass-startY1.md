# Rendering Bug: Black line on "Bass" at startY=1

## Symptom
- A black 2-3px wide line drawn over the top of the first "s" in "Bass"
- Only when `startY=1`
- Not at `startY=0`, `startY=2`, or any other value
- Not with different param names — only "Bass"

## What was investigated

### User's drawing code (ui.js)
Both layout functions reviewed (`drawParamsClassic` at line 511, `drawParamsCompact` at line 558). Drawing order is correct in both:
- `clear_screen()` zeros everything
- Text drawn with `print(x, y, name, 1)` (white pixels only)
- Value bars drawn at `barX = x + 44` (Layout A) or `barY = y + 8` (Layout B) — no overlap with text area
- Layout B uses `fill_rect(x, y-1, colW, 9, 0)` to clear before drawing — correct
- Layout B uses clip rects to isolate columns — correct
- `drawFooter()` draws at y=53+ — no overlap
- No other drawing touches the y=1 text area

No drawing operation in the module writes black pixels over the "Bass" text position.

### Schwung core rendering pipeline (two separate paths)

**Host path** (schwung_host.c — standalone mode):
- Own `screen_buffer[128*64]`, `set_pixel()`, `clear_screen()`, `print()`, `glyph()`
- Own `push_screen()` packing + slice-based SPI transfer
- `glyph()` at line 513: iterates font bitmap, calls `set_pixel(sx+x, sy+y, color)` for set pixels only
- `print()` at line 529: iterates string, calls `glyph()` per character
- `push_screen()` at line 2543: packs buffer to SSD1306 page format, sends 6 slices
- No clip rect support in host

**Shadow UI path** (shadow_ui.c + js_display.c — shadow mode, likely the active path):
- `js_display_screen_buffer[128*64]`, `js_display_set_pixel()`, `js_display_clear()`, `js_display_print()`, `js_display_glyph()`
- `js_display_pack()` at js_display.c:163 — packs to SSD1306 format
- Packed buffer copied to `shadow_display_shm` shared memory
- Shim's `shadow_swap_display()` reads from shm and writes slices to SPI mailbox
- Has clip rect support (set_clip_rect / clear_clip_rect)

Both rendering paths' glyph functions are correct — they only write SET pixels (where font bitmap data is 1), never clear pixels.

### Pack function (js_display.c:163)
```c
void js_display_pack(uint8_t *dest) {
    for (int y = 0; y < 64/8; y++) {        // pages 0-7
        for (int x = 0; x < 128; x++) {     // columns
            int index = (y * 128 * 8) + x;
            unsigned char packed = 0;
            for (int j = 0; j < 8; j++) {
                int packIndex = index + j * 128;
                packed |= screen_buffer[packIndex] << j;
            }
            dest[i++] = packed;
        }
    }
}
```
Correct SSD1306 page-column packing. No position-dependent or content-dependent bugs.

### Font data (generate_font.py)
Font is 5x7 bitmap. Relevant glyphs:
```
'B':         'a':         's':
####.        .....        .....
#...#        .....        .....
#...#        .###.        .###.
####.        ....#        #....
#...#        .####        .###.
#...#        #...#        ....#
####.        .####        ####.
```

Font loaded from PNG atlas with auto-trim (trims empty columns per glyph). charSpacing=1.

### Shim display compositing (schwung_shim.c)
- `shadow_swap_display()` at line 2534: copies shadow display to SPI mailbox in 7 phases (1 clear + 6 slices)
- Recording dot overlay at (123, 1, 4x4) — far from text, only when recording
- Skipback toast / shift+knob overlay — centered boxes, content-independent
- No overlays that depend on display content or y-position

### Shadow overlay (shadow_overlay.c)
- `overlay_fill_rect()` operates on packed SSD1306 buffer
- Only draws recording dot, shift+knob info, skipback toast
- None of these overlap with the y=0-10 text area at x=0-30

## What was NOT found
No bug was identified in the Schwung core code. The rendering pipeline (glyph rendering, buffer packing, display transfer, overlay compositing) is correct for all y positions and text content.

## Theories still open

1. **Font glyph pixel interaction at y=1**: The 's' glyph has empty rows 0-1 (lowercase descender). At y=1, the visible part of 's' starts at screen row 3 (y=1 + glyph row 2). There may be a sub-pixel alignment issue specific to this position that creates a visual artifact on the physical OLED but isn't a buffer-level bug.

2. **SSD1306 hardware artifact**: Page 0 covers rows 0-7. At y=1, the 's' glyph straddles specific bit positions in page 0 bytes. There could be a display controller issue with certain bit patterns at certain page positions. The fact that y=0 and y=2 work but y=1 doesn't suggests a hardware-level sensitivity.

3. **Race condition in shm display transfer**: The shadow_ui writes the full 1024-byte packed buffer to `shadow_display_shm` via memcpy. The shim reads from it one slice at a time (172 bytes per ioctl). No mutex protects this. If the shadow_ui overwrites shm mid-slice-sequence, you could get a frame where some slices are from tick N and others from tick N+1. However, this would cause random tearing, not a consistent position+content-dependent artifact.

4. **Something outside the reviewed code**: There may be another process or overlay writing to the display that wasn't found in this analysis.

## Suggested next steps

- **Pixel dump**: Add debug logging to dump the screen buffer around the affected area (x=10-20, y=0-5) after `drawScreen()` completes, to verify whether the artifact is in the buffer or only on the physical display.
- **Test with shadow overlay disabled**: Check if any overlay is unexpectedly active.
- **Test in standalone host mode** (not shadow mode) to see if the artifact reproduces — this would isolate whether it's in the rendering code vs the shm/shim path.
- **Inspect the actual font.png on device** to rule out atlas corruption.
