#ifdef _WIN32
#define USB_LUT_EXPORT __declspec(dllexport)
#else
#define USB_LUT_EXPORT
#endif

#ifdef _MSC_VER
typedef unsigned __int64 usb_size_t;
#else
typedef unsigned long long usb_size_t;
#endif

static int lut_bits(int levels)
{
    int bits = 0;
    int value = 1;
    while (value < levels && bits < 8) {
        value <<= 1;
        bits++;
    }
    return value == levels ? bits : -1;
}

static int apply_lut3d(
    const unsigned char *src,
    unsigned char *dst,
    int width,
    int height,
    const unsigned char *lut,
    int levels,
    int src_is_bgr)
{
    if (src == 0 || dst == 0 || lut == 0) {
        return -1;
    }
    if (width <= 0 || height <= 0 || levels <= 0 || levels > 256) {
        return -2;
    }

    const int bits = lut_bits(levels);
    if (bits <= 0) {
        return -3;
    }

    const int shift = 8 - bits;
    const usb_size_t pixels = (usb_size_t)width * (usb_size_t)height;

    for (usb_size_t i = 0, p = 0; i < pixels; i++, p += 3) {
        const unsigned char c0 = src[p];
        const unsigned char c1 = src[p + 1];
        const unsigned char c2 = src[p + 2];
        const unsigned int r = (unsigned int)((src_is_bgr ? c2 : c0) >> shift);
        const unsigned int g = (unsigned int)(c1 >> shift);
        const unsigned int b = (unsigned int)((src_is_bgr ? c0 : c2) >> shift);
        const usb_size_t index = (((usb_size_t)r * (usb_size_t)levels + (usb_size_t)g) * (usb_size_t)levels + (usb_size_t)b) * 3u;

        dst[p] = lut[index];
        dst[p + 1] = lut[index + 1];
        dst[p + 2] = lut[index + 2];
    }

    return 0;
}

USB_LUT_EXPORT int usb_lut3d_apply_rgb(
    const unsigned char *src_rgb,
    unsigned char *dst_rgb,
    int width,
    int height,
    const unsigned char *lut_rgb_flat,
    int levels)
{
    return apply_lut3d(src_rgb, dst_rgb, width, height, lut_rgb_flat, levels, 0);
}

USB_LUT_EXPORT int usb_lut3d_apply_bgr(
    const unsigned char *src_bgr,
    unsigned char *dst_rgb,
    int width,
    int height,
    const unsigned char *lut_rgb_flat,
    int levels)
{
    return apply_lut3d(src_bgr, dst_rgb, width, height, lut_rgb_flat, levels, 1);
}
