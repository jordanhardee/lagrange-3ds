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
    # libctru userAppExit runs after newlib's atexit and stdio cleanup.
    # Keep mounted ROMFS available to late file destructors. Other SDL
    # executables (including the independent smoke test) retain ownership.
    edit(root / "src/main/n3ds/SDL_sysmain_runapp.c", [
        ("int SDL_RunApp(", """extern bool lagrange_3ds_take_romfs_ownership(void) __attribute__((weak));

int SDL_RunApp("""),
        ("    romfsInit();", """    const bool mounted = R_SUCCEEDED(romfsInit());
    const bool deferred = mounted && lagrange_3ds_take_romfs_ownership &&
                          lagrange_3ds_take_romfs_ownership();"""),
        ("    romfsExit();", "    if (mounted && !deferred) romfsExit();"),
    ])
    # Keep SDL's RGBA8888 logical canvas but use native BGR8 scanout.
    # Azahar's GSP SaveVramSysArea explicitly clears 4-byte framebuffer
    # formats. libctru's default BGR8 also matches normal applet capture.
    edit(root / "src/video/n3ds/SDL_n3dsvideo.c", [
        ("    gfxInit(GSP_RGBA8_OES, GSP_RGBA8_OES, false);",
         """    if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false))
        gfxInit(GSP_BGR8_OES, GSP_BGR8_OES, false);
    else
        gfxInit(GSP_RGBA8_OES, GSP_RGBA8_OES, false);"""),
        ("""    gfxSetScreenFormat(displaydata->screen, modedata->fmt);
    if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false))
        gfxSetScreenFormat(GFX_BOTTOM, modedata->fmt);""",
         """    if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false)) {
        gfxSetScreenFormat(GFX_TOP, GSP_BGR8_OES);
        gfxSetScreenFormat(GFX_BOTTOM, GSP_BGR8_OES);
    } else {
        gfxSetScreenFormat(displaydata->screen, modedata->fmt);
    }"""),
    ])
    # A guest-side marker proves the dual-display update path was reached.
    edit(root / "src/video/n3ds/SDL_n3dsframebuffer.c", [
        ('#include "SDL_n3dsvideo.h"', '#include "SDL_n3dsvideo.h"\n#include <3ds/svc.h>\n#include <3ds/services/gspgpu.h>\n#include <stdio.h>'),
        ("        for (int screen = GFX_TOP; screen <= GFX_BOTTOM; ++screen) {",
         """        /* The libctru framebuffer swap takes effect on VBlank.
           Never reuse a pending/front buffer before the previous present. */
        /* APT may temporarily revoke GPU rights (HOME or keyboard).
           Waiting for VBlank alone does not prove either queued swap has
           been consumed. Never overwrite a buffer still pending display. */
        if (!gspHasGpuRight()) return true;
        const Uint64 deadline = SDL_GetTicks() + 100;
        while (gspIsPresentPending(GFX_TOP) || gspIsPresentPending(GFX_BOTTOM)) {
            if (!gspHasGpuRight()) return true;
            if (SDL_GetTicks() >= deadline)
                return SDL_SetError("3DS framebuffer presentation timed out");
            SDL_Delay(1);
        }
        for (int screen = GFX_TOP; screen <= GFX_BOTTOM; ++screen) {"""),
        ("            u32 *dst = (u32 *)gfxGetFramebuffer((gfxScreen_t)screen, GFX_LEFT, &fbw, &fbh);",
         """            u8 *dst = gfxGetFramebuffer((gfxScreen_t)screen, GFX_LEFT, &fbw, &fbh);
            if (gfxGetScreenFormat((gfxScreen_t)screen) != GSP_BGR8_OES)
                return SDL_SetError("Unexpected 3DS scanout format");"""),
        ("                    dst[GetDestOffset(x, y, fbw)] = src[sx];",
         """                    /* RGBA8888 is a packed 0xRRGGBBAA word. The
                       native BGR8 framebuffer stores B, G, R bytes. */
                    const u32 pixel = src[sx];
                    u8 *out = dst + 3 * GetDestOffset(x, y, fbw);
                    out[0] = (u8)(pixel >> 8);
                    out[1] = (u8)(pixel >> 16);
                    out[2] = (u8)(pixel >> 24);"""),
        ("            FlushN3DSBuffer(dst, fbw * fbh * sizeof(u32), (gfxScreen_t)screen);\n        }\n        return true;",
         """            GSPGPU_FlushDataCache(dst, fbw * fbh * 3u);
        }
        /* Submit both completed buffers as one dual-screen presentation. */
        gfxSwapBuffers();
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

    # libctru APT runs at 0x31; SDL normally creates workers at the
    # caller's priority (usually 0x30). On the single usable Old 3DS
    # application core a runnable worker can starve APT. Limit browser
    # SDL workers to 0x32 or lower urgency; do NOT globally wrap or
    # demote libctru service threads or change main-thread priority.
    edit(root / "src/thread/n3ds/SDL_systhread.c", [
        ("    svcGetThreadPriority(&priority, CUR_THREAD_HANDLE);",
         """    svcGetThreadPriority(&priority, CUR_THREAD_HANDLE);
    if (SDL_GetHintBoolean("LAGRANGE_3DS_DUALSCREEN", false) && priority < 0x32)
        priority = 0x32;"""),
    ])

    # Poll APT before touching the input service; after HOME-close, do not
    # dispatch stale touch events into a browser that is about to shut down.
    edit(root / "src/video/n3ds/SDL_n3dsevents.c", [
        ("""    hidScanInput();
    N3DS_PollTouch(_this);

    if (!aptMainLoop()) {""",
         """    if (!aptMainLoop()) {"""),
        ("""        SDL_PushEvent(&ev);
        return;
    }
}""",
         """        SDL_PushEvent(&ev);
        return;
    }
    hidScanInput();
    N3DS_PollTouch(_this);
}"""),
    ])

    # ARM EABI uses short enums. Lagrange carries private scroll flags in
    # direction, so preserve the 32-bit representation across SDL and app.
    edit(root / "include/SDL3/SDL_mouse.h", [
        ("    SDL_MOUSEWHEEL_FLIPPED    /**< The scroll direction is flipped / natural */",
         """    SDL_MOUSEWHEEL_FLIPPED,   /**< The scroll direction is flipped / natural */
    SDL_MOUSEWHEEL_N3DS_PRIVATE_FLAGS = 0x7fffffff /* keep enum 32 bits */"""),
    ])
    # A touch used to dismiss the system applet must not become a fresh
    # browser tap on resume. Preserve the release boundary in SDL's driver.
    edit(root / "src/video/n3ds/SDL_n3dstouch.c", [
        ("    if (pressed != was_pressed) {",
         """    if (SDL_GetHintBoolean("LAGRANGE_3DS_TOUCH_REARM", false)) {
        if (was_pressed) {
            SDL_SendTouch(0, N3DS_TOUCH_ID, N3DS_TOUCH_FINGER, window,
                          SDL_EVENT_FINGER_CANCELED, last_x, last_y, 0.0f);
            was_pressed = false;
        }
        if (!pressed) SDL_SetHint("LAGRANGE_3DS_TOUCH_REARM", "0");
        return;
    }
    if (pressed != was_pressed) {"""),
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
#include <3ds/services/apt.h>
#include <3ds/svc.h>
#include <SDL3/SDL_hints.h>
#include <string.h>

/* The keyboard is modal and may suspend the application. Only one launch
   may be outstanding; ignore stale focus notifications just after return. */
static iBool nativeKeyboardActive_3ds_;
static uint64_t nativeKeyboardClosed_3ds_;
static void keyboardTrace_3ds_(const char *message) {
    svcOutputDebugString(message, strlen(message));
}
static void keyboardAptHook_3ds_(APT_HookType type, void *unused) {
    (void) unused;
    if (type == APTHOOK_ONSUSPEND)
        keyboardTrace_3ds_("LAGRANGE_3DS_SWKBD_APT_SUSPEND");
    else if (type == APTHOOK_ONRESTORE)
        keyboardTrace_3ds_("LAGRANGE_3DS_SWKBD_APT_RESTORE");
}
#endif"""),
        ("    if (isCommand_Widget(w, ev, \"focus.gained\")) {",
         """#ifdef __3DS__
    /* Run the modal applet as a separate command after normal focus
       dispatch has completed. A stale request cannot reopen the applet. */
    if (isCommand_Widget(w, ev, "n3ds.keyboard.open")) {
        if (!isFocused_Widget(w) || !isEditing_InputWidget_(d) ||
            nativeKeyboardActive_3ds_) return iTrue;
        const iString *initial = text_InputWidget(d);
        const int maxChars = d->maxLen > 0 && d->maxLen < 1024 ? d->maxLen : 1024;
        if (size_String(initial) >= 1024) return iTrue;
        SwkbdState keyboard;
        char result[4096] = {0};
        swkbdInit(&keyboard, SWKBD_TYPE_NORMAL, 2, maxChars);
        swkbdSetInitialText(&keyboard, cstr_String(initial));
        swkbdSetButton(&keyboard, SWKBD_BUTTON_LEFT, "Cancel", false);
        swkbdSetButton(&keyboard, SWKBD_BUTTON_CONFIRM, "OK", true);
        nativeKeyboardActive_3ds_ = iTrue;
        keyboardTrace_3ds_("LAGRANGE_3DS_SWKBD_OPEN");
        aptHookCookie cookie;
        aptHook(&cookie, keyboardAptHook_3ds_, NULL);
        const SwkbdButton button = swkbdInputText(&keyboard, result, sizeof(result));
        aptUnhook(&cookie);
        nativeKeyboardClosed_3ds_ = SDL_GetTicks();
        SDL_SetHint("LAGRANGE_3DS_TOUCH_REARM", "1");
        char marker[96];
        snprintf(marker, sizeof(marker), "LAGRANGE_3DS_SWKBD_RETURN button:%d result:%d",
                 (int) button, (int) keyboard.result);
        keyboardTrace_3ds_(marker);
        if (button == SWKBD_BUTTON_CONFIRM) {
            setTextUndoableCStr_InputWidget(d, result, iTrue);
            contentsWereChanged_InputWidget_(d);
            if ((d->inFlags & isUrl_InputWidgetFlag) &&
                !cmp_String(id_Widget(w), "url"))
                d->inFlags |= enterPressed_InputWidgetFlag;
            end_InputWidget(d, iTrue);
            keyboardTrace_3ds_("LAGRANGE_3DS_SWKBD_CONFIRM");
        }
        else if (button == SWKBD_BUTTON_LEFT) {
            end_InputWidget(d, iFalse);
            keyboardTrace_3ds_("LAGRANGE_3DS_SWKBD_CANCEL");
        }
        memset(result, 0, sizeof(result));
        /* An applet error leaves the themed editor usable. Never treat
           an invalid return code as confirmation or as new input. */
        if (button == SWKBD_BUTTON_CONFIRM || button == SWKBD_BUTTON_LEFT) {
            if (isFocused_Widget(w)) setFocus_Widget(NULL);
        }
        nativeKeyboardActive_3ds_ = iFalse;
        postRefresh_Window(window_Widget(w));
        keyboardTrace_3ds_("LAGRANGE_3DS_SWKBD_RESUME");
        return iTrue;
    }
#endif
    if (isCommand_Widget(w, ev, "focus.gained")) {"""),
        ("            begin_InputWidget_(d, method != arrowKeys_FocusMethod);",
         """#ifdef __3DS__
            if (!isFocused_Widget(w)) return iFalse;
            const iBool alreadyEditing = isEditing_InputWidget_(d);
            const iBool useNative = !alreadyEditing &&
                method != arrowKeys_FocusMethod &&
                !isAllowedToInsertNewline_InputWidget_(d) &&
                !(d->inFlags & isSensitive_InputWidgetFlag) &&
                size_String(text_InputWidget(d)) < 1024;
            if (useNative && (nativeKeyboardActive_3ds_ ||
                (nativeKeyboardClosed_3ds_ &&
                 SDL_GetTicks() - nativeKeyboardClosed_3ds_ < 350))) {
                setFocus_Widget(NULL);
                return iFalse;
            }
#endif
            begin_InputWidget_(d, method != arrowKeys_FocusMethod);
#ifdef __3DS__
            if (useNative) postCommand_Widget(w, "n3ds.keyboard.open");
#endif"""),
    ])

    # A lower-priority service/worker needs a scheduling opportunity even
    # when input or animation keeps the UI continuously busy. This is a
    # real blocking sleep, not a same-priority yield.
    edit(root / "app/src/app/events.c", [
        ("        iterate_App_(d);",
         """        iterate_App_(d);
#ifdef __3DS__
        if (d->isRunning) SDL_Delay(1);
#endif"""),
    ])
    # Our display backend already paces presentation against pending GSP
    # swaps. SDL software VSync emulation adds another delay on the old 3DS.
    # Surface errors must be visible in the existing startup log, not hidden
    # by an unchecked SDL_RenderPresent return value.
    edit(root / "app/src/ui/window.c", [
        ("    SDL_SetRenderVSync(d->render, 1);",
         """#ifdef __3DS__
    SDL_SetRenderVSync(d->render, 0);
#else
    SDL_SetRenderVSync(d->render, 1);
#endif"""),
        ("    SDL_RenderPresent(render);",
         """#ifdef __3DS__
    if (!SDL_RenderPresent(render)) {
        static iBool reported;
        if (!reported) {
            fprintf(stderr, "[3DS] SDL_RenderPresent: %s\\n", SDL_GetError());
            FILE *log = fopen("sdmc:/3ds/lagrange/cache/startup.log", "a");
            if (log) {
                fprintf(log, "LAGRANGE_3DS_PRESENT_FAILED %s\\n", SDL_GetError());
                fclose(log);
            }
            reported = iTrue;
        }
    }
#else
    SDL_RenderPresent(render);
#endif"""),
    ])
    # The workflow currently inserts early socExit calls into main(). They
    # unregister the device while newlib can still have live descriptors.
    # Move service ownership to libctru's late application-exit hook.
    main = root / "app/src/main.c"
    source = main.read_text()
    assert source.count("exitSoc_3ds_();") == 6, "unexpected SOC exit sites"
    source = source.replace("exitSoc_3ds_();", "/* SOC released by userAppExit after stdio cleanup. */")
    anchor = "int main(int argc, char **argv) {"
    assert source.count(anchor) == 1
    source = source.replace(anchor, """#ifdef __3DS__
#include <3ds/romfs.h>
static bool romfsOwned_3ds_;

bool lagrange_3ds_take_romfs_ownership(void) {
    romfsOwned_3ds_ = true;
    return true;
}

/* Called by libctru after C exit handlers and stdio cleanup, but before
   FS and service-manager shutdown. Do not log to files from this hook. */
void userAppExit(void) {
    exitSoc_3ds_();
    if (romfsOwned_3ds_) {
        romfsExit();
        romfsOwned_3ds_ = false;
    }
}
#endif

""" + anchor)
    main.write_text(source)


def debug_sdl(root):
    # SDL is also used by standalone probes; the browser's logging sink is optional.
    edit(root / "src/video/n3ds/SDL_n3dsframebuffer.c", [
        ("#include <stdio.h>", """#include <stdio.h>
extern void lagrange_3ds_debug(const char *, unsigned, unsigned, unsigned)
    __attribute__((weak));"""),
        ("        if (!gspHasGpuRight()) return true;\n        const Uint64 deadline",
         """        static Uint64 lastSample;
        const Uint64 begin = SDL_GetTicks();
        const bool sample = !lastSample || begin - lastSample >= 2000;
        if (sample) {
            lastSample = begin;
            if (lagrange_3ds_debug)
                lagrange_3ds_debug("FB_BEGIN gpu/topPending/bottomPending",
                    gspHasGpuRight(), gspIsPresentPending(GFX_TOP),
                    gspIsPresentPending(GFX_BOTTOM));
        }
        if (!gspHasGpuRight()) return true;
        const Uint64 deadline"""),
        ("        for (int screen = GFX_TOP; screen <= GFX_BOTTOM; ++screen) {",
         """        const Uint64 copyStart = SDL_GetTicks();
        for (int screen = GFX_TOP; screen <= GFX_BOTTOM; ++screen) {"""),
        ("        gfxSwapBuffers();\n        static bool reported",
         """        gfxSwapBuffers();
        if (sample && lagrange_3ds_debug)
            lagrange_3ds_debug("FB_END waitMs/copySwapMs/format",
                (unsigned)(copyStart - begin), (unsigned)(SDL_GetTicks() - copyStart),
                (unsigned)gfxGetScreenFormat(GFX_TOP));
        static bool reported"""),
    ])
    edit(root / "src/video/n3ds/SDL_n3dsevents.c", [
        ("#include <3ds.h>", """#include <3ds.h>
extern void lagrange_3ds_debug(const char *, unsigned, unsigned, unsigned)
    __attribute__((weak));"""),
        ("    if (!aptMainLoop()) {", """    if (!aptMainLoop()) {
        static bool reportedClose;
        if (!reportedClose && lagrange_3ds_debug) {
            lagrange_3ds_debug("APT_MAINLOOP_CLOSE", 0, 0, 0);
            reportedClose = true;
        }"""),
    ])

def debug_app(root):
    main = root / "app/src/main.c"
    edit(main, [
        ("void userAppExit(void) {",
         """void userAppExit(void) {
    if (debugHooked_3ds_) {
        aptUnhook(&debugHook_3ds_);
        debugHooked_3ds_ = false;
    }"""),
        ("static void trace_3ds_(const char *message) {",
         """/* Diagnostic build: bounded, local, numeric-only logging. Close each
   record so ordinary crashes leave a usable file. TryLock avoids blocking
   a system callback on another thread's diagnostic I/O. */
#include <3ds/os.h>
#include <3ds/allocator/linear.h>
#include <3ds/services/apt.h>
#include <3ds/synchronization.h>
#include <malloc.h>
#define DEBUG_PATH_3DS "sdmc:/3ds/lagrange/debug.log"
#define DEBUG_PREV_3DS "sdmc:/3ds/lagrange/debug-prev.log"
#define DEBUG_LIMIT_3DS (256u * 1024u)
static LightLock debugLock_3ds_ = 1;
static bool debugEnabled_3ds_;
static unsigned debugBytes_3ds_;
static u64 debugStartTick_3ds_;
static aptHookCookie debugHook_3ds_;
static bool debugHooked_3ds_;

void lagrange_3ds_debug(const char *event, unsigned a, unsigned b, unsigned c) {
    if (LightLock_TryLock(&debugLock_3ds_) != 0) return;
    if (debugEnabled_3ds_ && debugBytes_3ds_ < DEBUG_LIMIT_3DS - 256) {
        FILE *file = fopen(DEBUG_PATH_3DS, "a");
        if (file) {
            unsigned long long ms = (svcGetSystemTick() - debugStartTick_3ds_) /
                                    (SYSCLOCK_ARM11 / 1000u);
            int n = fprintf(file, "%llu %s %u %u %u\\n", ms, event, a, b, c);
            if (n > 0) debugBytes_3ds_ += (unsigned)n;
            fclose(file);
        }
    }
    LightLock_Unlock(&debugLock_3ds_);
}
static void debugApt_3ds_(APT_HookType type, void *unused) {
    (void)unused;
    switch (type) {
        case APTHOOK_ONSUSPEND: lagrange_3ds_debug("APT_SUSPEND", 0, 0, 0); break;
        case APTHOOK_ONRESTORE: lagrange_3ds_debug("APT_RESTORE", 0, 0, 0); break;
        case APTHOOK_ONSLEEP: lagrange_3ds_debug("APT_SLEEP", 0, 0, 0); break;
        case APTHOOK_ONWAKEUP: lagrange_3ds_debug("APT_WAKE", 0, 0, 0); break;
        case APTHOOK_ONEXIT: lagrange_3ds_debug("APT_EXIT", 0, 0, 0); break;
        default: break;
    }
}
static void debugStart_3ds_(void) {
    if (debugEnabled_3ds_) return;
    struct stat st;
    if (stat(DEBUG_PATH_3DS, &st) == 0) {
        remove(DEBUG_PREV_3DS);
        if (rename(DEBUG_PATH_3DS, DEBUG_PREV_3DS) != 0) return;
    }
    FILE *file = fopen(DEBUG_PATH_3DS, "w");
    if (!file) return;
    fclose(file);
    LightLock_Lock(&debugLock_3ds_);
    debugStartTick_3ds_ = svcGetSystemTick();
    debugEnabled_3ds_ = true;
    LightLock_Unlock(&debugLock_3ds_);
    bool new3ds = false;
    s32 priority = -1;
    APT_CheckNew3DS(&new3ds);
    svcGetThreadPriority(&priority, CUR_THREAD_HANDLE);
    lagrange_3ds_debug("DEBUG_START schema/new3ds/mainPriority", 1, new3ds, (unsigned)priority);
    aptHook(&debugHook_3ds_, debugApt_3ds_, NULL);
    debugHooked_3ds_ = true;
}
void lagrange_3ds_debug_memory(void) {
    const struct mallinfo m = mallinfo();
    lagrange_3ds_debug("MEMORY heapUsed/heapFreeInArena/linearFree",
                       (unsigned)m.uordblks, (unsigned)m.fordblks, linearSpaceFree());
}
static void trace_3ds_(const char *message) {
    /* Only fixed milestone strings (not OpenSSL's arbitrary error text). */
    if (strncmp(message, "LAGRANGE_3DS_", 13) == 0)
        lagrange_3ds_debug(message, 0, 0, 0);"""),
        ("    if (!initSoc_3ds_()) {",
         """    debugStart_3ds_();
    lagrange_3ds_debug("SOC_INIT_BEGIN", 0, 0, 0);
    if (!initSoc_3ds_()) {
        lagrange_3ds_debug("SOC_INIT_FAILED", 0, 0, 0);"""),
        ("    /* Foundation assumes SSL_CTX_new succeeds.",
         """    debugStart_3ds_();
    lagrange_3ds_debug("SOC_STORAGE_READY", 0, 0, 0);
    lagrange_3ds_debug_memory();
    /* Foundation assumes SSL_CTX_new succeeds."""),
        ("    if (!SDL_Init(SDL_INIT_VIDEO)) {",
         """#ifdef __3DS__
    lagrange_3ds_debug("SDL_INIT_BEGIN", 0, 0, 0);
#endif
    if (!SDL_Init(SDL_INIT_VIDEO)) {
#ifdef __3DS__
        lagrange_3ds_debug("SDL_INIT_FAILED", 0, 0, 0);
#endif"""),
        ("    init_Updater();\n    run_App(argc, argv);\n    SDL_Quit();",
         """#ifdef __3DS__
    lagrange_3ds_debug("SDL_INIT_OK", 0, 0, 0);
#endif
    init_Updater();
    run_App(argc, argv);
#ifdef __3DS__
    lagrange_3ds_debug("APP_RETURN_SDL_QUIT_BEGIN", 0, 0, 0);
#endif
    SDL_Quit();
#ifdef __3DS__
    lagrange_3ds_debug("SDL_QUIT_END", 0, 0, 0);
#endif"""),
        ("    deinit_Updater();\n    deinit_Core();\n    deinit_Foundation();",
         """    deinit_Updater();
#ifdef __3DS__
    lagrange_3ds_debug("CORE_DEINIT_BEGIN", 0, 0, 0);
#endif
    deinit_Core();
#ifdef __3DS__
    lagrange_3ds_debug("FOUNDATION_DEINIT_BEGIN", 0, 0, 0);
#endif
    deinit_Foundation();
#ifdef __3DS__
    lagrange_3ds_debug("FOUNDATION_DEINIT_END", 0, 0, 0);
    if (debugHooked_3ds_) {
        aptUnhook(&debugHook_3ds_);
        debugHooked_3ds_ = false;
    }
    lagrange_3ds_debug("MAIN_RETURN", 0, 0, 0);
#endif"""),
    ])
    edit(root / "app/src/ui/inputwidget.c", [
        ("static void keyboardTrace_3ds_(const char *message) {",
         """extern void lagrange_3ds_debug(const char *, unsigned, unsigned, unsigned);
static void keyboardTrace_3ds_(const char *message) {
    /* The caller supplies fixed lifecycle markers or numeric result codes,
       never initial text, submitted text, or the URL. */
    lagrange_3ds_debug(message, 0, 0, 0);"""),
    ])
    edit(root / "app/src/app/startup.c", [
        ("int run_App(int argc, char **argv) {",
         """#ifdef __3DS__
extern void lagrange_3ds_debug(const char *, unsigned, unsigned, unsigned);
#endif
int run_App(int argc, char **argv) {
#ifdef __3DS__
    lagrange_3ds_debug("APP_INIT_BEGIN", 0, 0, 0);
#endif"""),
        ("    const int rc = run_App_(&app_);\n    deinit_App_(&app_);",
         """#ifdef __3DS__
    lagrange_3ds_debug("APP_INIT_OK_LOOP_BEGIN", 0, 0, 0);
#endif
    const int rc = run_App_(&app_);
#ifdef __3DS__
    lagrange_3ds_debug("APP_LOOP_END_DEINIT_BEGIN", (unsigned)rc, 0, 0);
#endif
    deinit_App_(&app_);
#ifdef __3DS__
    lagrange_3ds_debug("APP_DEINIT_END", 0, 0, 0);
#endif"""),
    ])
    edit(root / "app/src/app/events.c", [
        ("static void iterate_App_(void *app) {",
         """#ifdef __3DS__
extern void lagrange_3ds_debug(const char *, unsigned, unsigned, unsigned);
extern void lagrange_3ds_debug_memory(void);
#endif
static void iterate_App_(void *app) {
#ifdef __3DS__
    static Uint64 nextSample;
    const Uint64 t0 = SDL_GetTicks();
    const iBool sample = t0 >= nextSample;
    if (sample) {
        nextSample = t0 + 2000;
        lagrange_3ds_debug("LOOP_BEGIN", 0, 0, 0);
        lagrange_3ds_debug_memory();
    }
#endif"""),
        ("    processEvents_App(waitForNewEvents_AppEventMode);\n    endPhase_FrameTiming(events_FramePhase);",
         """    processEvents_App(waitForNewEvents_AppEventMode);
#ifdef __3DS__
    const Uint64 t1 = SDL_GetTicks();
    if (sample) lagrange_3ds_debug("EVENTS_END ms", (unsigned)(t1 - t0), 0, 0);
#endif
    endPhase_FrameTiming(events_FramePhase);"""),
        ("    refresh_App();\n    endPhase_FrameTiming(draw_FramePhase);",
         """#ifdef __3DS__
    const Uint64 t2 = SDL_GetTicks();
    if (sample) lagrange_3ds_debug("DRAW_BEGIN tickersMs", (unsigned)(t2 - t1), 0, 0);
#endif
    refresh_App();
#ifdef __3DS__
    if (sample) lagrange_3ds_debug("DRAW_END ms", (unsigned)(SDL_GetTicks() - t2), 0, 0);
#endif
    endPhase_FrameTiming(draw_FramePhase);"""),
    ])
    edit(root / "app/src/ui/window.c", [
        ("static void present_Window_(SDL_Renderer *render) {",
         """#ifdef __3DS__
extern void lagrange_3ds_debug(const char *, unsigned, unsigned, unsigned);
#endif
static void present_Window_(SDL_Renderer *render) {"""),
        ("            reported = iTrue;\n        }\n    }\n#else\n    SDL_RenderPresent(render);",
         """            lagrange_3ds_debug("PRESENT_FAILED", 0, 0, 0);
            reported = iTrue;
        }
    }
#else
    SDL_RenderPresent(render);"""),
    ])
    # This source is compiled for the standalone TLS probe before app()
    # is run, so the browser-only instrumentation does not affect that probe.
    edit(root / "port/3ds/pthread-tls.c", [
        ("Thread __wrap_threadCreate(ThreadFunc entry, void *arg, size_t stack,",
         """extern void lagrange_3ds_debug(const char *, unsigned, unsigned, unsigned);
Thread __wrap_threadCreate(ThreadFunc entry, void *arg, size_t stack,"""),
        ("    if (!thread) free(start);\n    return thread;",
         """    if (!thread) free(start);
    lagrange_3ds_debug(thread ? "THREAD_CREATE priority/core/stack" : "THREAD_CREATE_FAILED priority/core/stack",
                       (unsigned)priority, (unsigned)core, (unsigned)stack);
    return thread;"""),
    ])

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("sdl", "app"):
        raise SystemExit("usage: runtime.py {sdl|app} SOURCE_ROOT")
    root = Path(sys.argv[2])
    if sys.argv[1] == "sdl":
        sdl(root)
        debug_sdl(root)
    else:
        app(root)
        debug_app(root)
