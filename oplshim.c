/* Tiny C glue so Python (ctypes) can drive Nuked-OPL3 without knowing the chip struct layout. */
#include <stdlib.h>
#include <stdint.h>
#include "opl3.h"

void *opl_new(uint32_t rate) {
    opl3_chip *c = calloc(1, sizeof(opl3_chip));
    OPL3_Reset(c, rate);
    return c;
}
void opl_free(void *c) { free(c); }
void opl_write(void *c, uint16_t reg, uint8_t v) { OPL3_WriteReg((opl3_chip *)c, reg, v); }
/* n stereo frames, interleaved int16, scaled by gain (headroom so busy songs don't clip) */
void opl_gen(void *c, int16_t *buf, int n, float gain) {
    OPL3_GenerateStream((opl3_chip *)c, buf, (uint32_t)n);
    for (int i = 0; i < n * 2; i++) {
        float v = buf[i] * gain;
        buf[i] = (int16_t)(v > 32767.f ? 32767 : (v < -32768.f ? -32768 : v));
    }
}
