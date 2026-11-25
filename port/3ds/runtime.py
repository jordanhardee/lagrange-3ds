#!/usr/bin/env python3
"""Runtime compatibility fixes for the pinned Nintendo 3DS build."""
import sys
from pathlib import Path

def edit(path, replacements):
    s = path.read_text()
    for old, new in replacements:
        assert s.count(old) == 1, (str(path), old, s.count(old))
        s = s.replace(old, new)
    path.write_text(s)

def sdl(root):
    # A guest-side marker proves the dual-display update path was reached.
    edit(root / "src/video/n3ds/SDL_n3dsframebuffer.c", [
        ('#include "SDL_n3dsvideo.h"', '#include "SDL_n3dsvideo.h"\n#include <3ds/svc.h>\n#include <stdio.h>'),
        ("            FlushN3DSBuffer(dst, fbw * fbh * sizeof(u32), (gfxScreen_t)screen);\n        }\n        return true;",
         """            FlushN3DSBuffer(dst, fbw * fbh * sizeof(u32), (gfxScreen_t)screen);
        }
        static bool reported = false;
        if (!reported) {
            static const char marker[] = "LAGRANGE_3DS_FIRST_FRAME";
            svcOutputDebugString(marker, sizeof(marker) - 1);
            FILE *proof = fopen("sdmc:/3ds/lagrange/cache/first-frame.ok", "w");
            if (proof) {
                fputs(marker, proof);
                fclose(proof);
            }
            reported = true;
        }
        return true;"""),
    ])

    # ARM EABI uses short enums. Lagrange carries private scroll flags in
    # direction, so preserve the 32-bit representation across SDL and app.
    edit(root / "include/SDL3/SDL_mouse.h", [
        ("    SDL_MOUSEWHEEL_FLIPPED    /**< The scroll direction is flipped / natural */",
         """    SDL_MOUSEWHEEL_FLIPPED,   /**< The scroll direction is flipped / natural */
    SDL_MOUSEWHEEL_N3DS_PRIVATE_FLAGS = 0x7fffffff /* keep enum 32 bits */"""),
    ])
    # Do not invoke a blocking native applet from SDL_StartTextInput.
    # Lagrange explicitly invokes it after preparing the input widget.
    edit(root / "src/video/n3ds/SDL_n3dsswkb.c", [
        ("    char buffer[BUFFER_SIZE];",
         """    if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false)) return true;
    char buffer[BUFFER_SIZE];"""),
    ])

def app(root):
    edit(root / "app/src/app/impl.h", [
        ("#elif defined (iPlatformLinux) || defined (iPlatformTerminal) || defined (iPlatformOther)",
         """#elif defined (__3DS__)
#define EMB_BIN "romfs:/resources.lgr"
#define defaultDataDir_App_ "/3ds/lagrange"

#elif defined (iPlatformLinux) || defined (iPlatformTerminal) || defined (iPlatformOther)"""),
    ])
    edit(root / "app/src/app/paths.c", [
        ('static const char *defaultDownloadDir_App_ = "~/Downloads";',
         """#ifdef __3DS__
static const char *defaultDownloadDir_App_ = "/3ds/lagrange/downloads";
#else
static const char *defaultDownloadDir_App_ = "~/Downloads";
#endif"""),
        ("const char *dataDir_App_(void) {\n",
         """const char *dataDir_App_(void) {
#ifdef __3DS__
    /* The SD device is selected during main(), before Foundation starts. */
    return defaultDataDir_App_;
#endif
"""),
        ("const char *downloadDir_App_(void) {\n",
         """const char *downloadDir_App_(void) {
#ifdef __3DS__
    return defaultDownloadDir_App_;
#endif
"""),
        ("#elif defined (P_tmpdir)\n",
         """#elif defined (__3DS__)
    iString *tmpPath = collectNew_String();
    const char *cache = "/3ds/lagrange/cache";
    const iRangecc tmpDir = range_CStr(cache);
#elif defined (P_tmpdir)
"""),
    ])
    edit(root / "app/src/main.c", [
        ("#include <signal.h>\n",
         """#include <signal.h>
#ifdef __3DS__
#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <3ds/svc.h>
#include <openssl/ssl.h>
#include <openssl/err.h>
#include <openssl/rand.h>
#include <openssl/provider.h>

/* This port uses the built-in default provider. Do not try loading a
   desktop OpenSSL configuration or modules from the cross-build prefix. */
static void trace_3ds_(const char *message) {
    svcOutputDebugString(message, strlen(message));
    FILE *log = fopen("sdmc:/3ds/lagrange/cache/startup.log", "a");
    if (log) {
        fputs(message, log);
        fputc('\\n', log);
        fclose(log);
    }
}
static int tlsError_3ds_(const char *text, size_t len, void *unused) {
    (void) unused;
    (void) len;
    trace_3ds_(text);
    return 1;
}
extern int lagrange_3ds_tsd_selftest(void);
static OSSL_PROVIDER *provider_3ds_;
static int checkTls_3ds_(void) {
    unsigned char random[32] = {0};
    SSL_CTX *ctx = NULL;
    trace_3ds_("LAGRANGE_3DS_TLS_INIT");
    if (!OPENSSL_init_ssl(OPENSSL_INIT_NO_LOAD_CONFIG | OPENSSL_INIT_LOAD_SSL_STRINGS, NULL))
        goto fail;
    trace_3ds_("LAGRANGE_3DS_TLS_INIT_OK");
    provider_3ds_ = OSSL_PROVIDER_load(NULL, "default");
    if (!provider_3ds_) goto fail;
    trace_3ds_("LAGRANGE_3DS_TLS_PROVIDER_OK");
    if (RAND_bytes(random, sizeof(random)) != 1) goto fail;
    OPENSSL_cleanse(random, sizeof(random));
    trace_3ds_("LAGRANGE_3DS_TLS_RNG_OK");
    ctx = SSL_CTX_new(TLS_client_method());
    if (!ctx) goto fail;
    if (!SSL_CTX_set_cipher_list(ctx,
        "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:"
        "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:"
        "ECDHE-RSA-CHACHA20-POLY1305:ECDHE-RSA-AES128-GCM-SHA256:"
        "DHE-RSA-AES256-GCM-SHA384")) goto fail;
    SSL_CTX_free(ctx);
    trace_3ds_("LAGRANGE_3DS_TLS_CONTEXT_OK");
    return 1;
fail:
    OPENSSL_cleanse(random, sizeof(random));
    trace_3ds_("LAGRANGE_3DS_TLS_FAILED");
    ERR_print_errors_cb(tlsError_3ds_, NULL);
    SSL_CTX_free(ctx);
    if (provider_3ds_) {
        OSSL_PROVIDER_unload(provider_3ds_);
        provider_3ds_ = NULL;
    }
    return 0;
}
#endif
"""),
        ("    init_Foundation();",
         """#ifdef __3DS__
    /* Use the SD card as the default device for POSIX absolute paths.
       ROMFS is read-only and is used exclusively for bundled resources. */
    const char *dirs[] = { "/3ds", "/3ds/lagrange",
                           "/3ds/lagrange/downloads", "/3ds/lagrange/cache" };
    if (chdir("sdmc:/") != 0) {
        fprintf(stderr, "[3DS] SD card unavailable\\n");
        exitSoc_3ds_();
        return 1;
    }
    for (size_t i = 0; i < sizeof(dirs)/sizeof(dirs[0]); ++i) {
        struct stat st;
        if ((mkdir(dirs[i], 0700) != 0 && errno != EEXIST) ||
            stat(dirs[i], &st) != 0 || !S_ISDIR(st.st_mode)) {
            fprintf(stderr, "[3DS] cannot initialize storage directory: %s\\n", dirs[i]);
            exitSoc_3ds_();
            return 1;
        }
    }
    /* Foundation assumes SSL_CTX_new succeeds. Fail before entering it
       if TLS cannot initialize; never continue with an invalid context. */
    FILE *log = fopen("sdmc:/3ds/lagrange/cache/startup.log", "w");
    if (log) fclose(log);
    trace_3ds_("LAGRANGE_3DS_STORAGE_OK");
    const int tsdStatus = lagrange_3ds_tsd_selftest();
    if (tsdStatus != 0) {
        char message[64];
        snprintf(message, sizeof(message), "LAGRANGE_3DS_TSD_FAILED %d", tsdStatus);
        trace_3ds_(message);
        exitSoc_3ds_();
        return 1;
    }
    trace_3ds_("LAGRANGE_3DS_TSD_OK");
    if (!checkTls_3ds_()) {
        fprintf(stderr, "[3DS] TLS initialization failed; see startup.log\\n");
        exitSoc_3ds_();
        return 1;
    }
#endif
    init_Foundation();"""),
        ("    init_Core();",
         """#ifdef __3DS__
    trace_3ds_("LAGRANGE_3DS_FOUNDATION_OK");
#endif
    init_Core();"""),
        ('    /* SDL configures per-monitor DPI awareness automatically. */',
         """#ifdef __3DS__
    trace_3ds_("LAGRANGE_3DS_CIPHERS_OK");
#endif
    /* SDL configures per-monitor DPI awareness automatically. */"""),
    ])
    edit(root / "app/src/app/startup.c", [
        ("        const char *paths[] = {\n",
         """        const char *paths[] = {
#ifdef __3DS__
            "romfs:/resources.lgr",
#endif
"""),
        ("    loadPrefs_App_(d);",
         """    loadPrefs_App_(d);
#ifdef __3DS__
    /* Extra fullscreen windows would compete for the same two displays. */
    d->prefs.detachedPrefs = iFalse;
#endif"""),
    ])
    edit(root / "app/src/ui/util.h", [
        ("enum iMouseWheelFlag {",
         """#ifdef __3DS__
_Static_assert(sizeof(SDL_MouseWheelDirection) == 4, "SDL scroll flags require a 32-bit enum");
_Static_assert((SDL_MouseWheelDirection)512 == 512 &&
               (SDL_MouseWheelDirection)1024 == 1024 &&
               (SDL_MouseWheelDirection)2048 == 2048, "SDL scroll flags truncated");
#endif
enum iMouseWheelFlag {"""),
    ])
    # A focused editor or open popup owns the Circle Pad, not the document.
    # Stop pending scroll immediately on switching modes.
    edit(root / "app/src/ui/gamepad.c", [
        ("            if (!d->panelFocus && axis->axis == SDL_GAMEPAD_AXIS_LEFTY) {",
         """            const iBool reading = !d->panelFocus && !isInputFocused_() &&
                                  isEmpty_PtrSet(d->openMenus);
            if (!reading && !d->panelFocus) {
                d->scrollSpeed = 0;
                d->scrollAccum = 0;
            }
            if (reading && axis->axis == SDL_GAMEPAD_AXIS_LEFTY) {"""),
        ("            if (!d->panelFocus && axis->axis == SDL_GAMEPAD_AXIS_LEFTX) return iTrue;",
         "            if (reading && axis->axis == SDL_GAMEPAD_AXIS_LEFTX) return iTrue;"),
        ("                    d->scrollSpeed = 0;\n                    d->pointerSpeed[0] = d->pointerSpeed[1] = 0;",
         "                    d->scrollSpeed = 0;\n                    d->scrollAccum = 0;\n                    d->pointerSpeed[0] = d->pointerSpeed[1] = 0;"),
    ])
    edit(root / "app/src/ui/inputwidget.c", [
        ('#include "input/impl.h"',
         """#include "input/impl.h"
#ifdef __3DS__
#include <3ds/applets/swkbd.h>
#include <string.h>
#endif"""),
        ("            begin_InputWidget_(d, method != arrowKeys_FocusMethod);",
         """#ifdef __3DS__
            const iBool alreadyEditing = isEditing_InputWidget_(d);
#endif
            begin_InputWidget_(d, method != arrowKeys_FocusMethod);
#ifdef __3DS__
            /* Single-line ordinary inputs use the system keyboard. Keep the
               themed editor for PEMs, uploads, sensitive input and long text. */
            if (!alreadyEditing && method != arrowKeys_FocusMethod &&
                !isAllowedToInsertNewline_InputWidget_(d) &&
                !(d->inFlags & isSensitive_InputWidgetFlag)) {
                const iString *initial = text_InputWidget(d);
                const int maxChars = d->maxLen > 0 && d->maxLen < 1024 ? d->maxLen : 1024;
                if (size_String(initial) < 1024) {
                    SwkbdState keyboard;
                    char result[4096] = {0};
                    swkbdInit(&keyboard, SWKBD_TYPE_NORMAL, 2, maxChars);
                    swkbdSetInitialText(&keyboard, cstr_String(initial));
                    const SwkbdButton button = swkbdInputText(&keyboard, result, sizeof(result));
                    if (button == SWKBD_BUTTON_CONFIRM) {
                        setTextUndoableCStr_InputWidget(d, result, iTrue);
                        contentsWereChanged_InputWidget_(d);
                        /* Submit the address field; other fields retain the
                           normal dialog-level confirmation controls. */
                        if ((d->inFlags & isUrl_InputWidgetFlag) &&
                            !cmp_String(id_Widget(w), "url"))
                            d->inFlags |= enterPressed_InputWidgetFlag;
                        end_InputWidget(d, iTrue);
                    }
                    else if (button == SWKBD_BUTTON_LEFT) {
                        end_InputWidget(d, iFalse);
                    }
                    memset(result, 0, sizeof(result));
                    /* Applet errors (including an emulator without keyboard support)
                       must leave the existing themed editor available. */
                    if (button == SWKBD_BUTTON_CONFIRM || button == SWKBD_BUTTON_LEFT)
                        setFocus_Widget(NULL);
                    postRefresh_Window(window_Widget(w));
                }
            }
#endif"""),
    ])

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("sdl", "app"):
        raise SystemExit("usage: runtime.py {sdl|app} SOURCE_ROOT")
    (sdl if sys.argv[1] == "sdl" else app)(Path(sys.argv[2]))
