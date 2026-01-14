import mss
import threading
import os
from io import BytesIO as _BytesIO

import Quartz  # type: ignore[import-not-found]
import AppKit  # type: ignore[import-not-found]


class ScreenshotRecorder:
    def __init__(self):
        self.sct = mss.mss()
        self.lock = threading.Lock()
        self._warned_quartz_missing = False
        self._warned_quartz_failed = False

    def _capture_quartz_below_window(self, filepath, *, below_window_id: int) -> str | None:
        """
        macOS-only: Capture the main display on-screen content *below* a given window id.

        This is used to keep an always-visible overlay from appearing in agent screenshots.
        """
        try:
            win_id = int(below_window_id)
            if win_id <= 0:
                return None

            # Access via getattr for PyObjC stub compatibility.
            display_id = getattr(Quartz, "CGMainDisplayID")()
            bounds = getattr(Quartz, "CGDisplayBounds")(display_id)

            cg_window_list_create_image = getattr(Quartz, "CGWindowListCreateImage")
            opt_below = getattr(Quartz, "kCGWindowListOptionOnScreenBelowWindow")
            img_default = getattr(Quartz, "kCGWindowImageDefault")
            cgimg = cg_window_list_create_image(
                bounds,
                opt_below,
                win_id,
                img_default,
            )
            if cgimg is None:
                return None

            # Convert CGImage -> PNG and write using AppKit.
            ns_bitmap_rep = getattr(AppKit, "NSBitmapImageRep")
            ns_png_file_type = getattr(AppKit, "NSPNGFileType")
            rep = ns_bitmap_rep.alloc().initWithCGImage_(cgimg)
            png = rep.representationUsingType_properties_(ns_png_file_type, None)
            if png is None:
                return None

            # Quartz capture returns backing-pixel resolution on Retina (e.g., 2940x1912),
            # but our executor/grounding historically operated in logical points (e.g., 1470x956).
            # Normalize the saved screenshot to display bounds (points) so coordinate mapping is consistent.
            try:
                src_w = int(getattr(Quartz, "CGImageGetWidth")(cgimg))
                src_h = int(getattr(Quartz, "CGImageGetHeight")(cgimg))
            except Exception:
                src_w = src_h = 0
            try:
                tgt_w = int(float(getattr(bounds, "size").width))  # type: ignore[attr-defined]
                tgt_h = int(float(getattr(bounds, "size").height))  # type: ignore[attr-defined]
            except Exception:
                try:
                    # Fallback: bounds is a CGRect-like tuple.
                    (_, _), (bw, bh) = bounds  # type: ignore[misc]
                    tgt_w, tgt_h = int(bw), int(bh)
                except Exception:
                    tgt_w = tgt_h = 0

            try:
                need_resize = bool(tgt_w and tgt_h and src_w and src_h and (src_w != tgt_w or src_h != tgt_h))
                if need_resize:
                    try:
                        from PIL import Image as _PILImage  # type: ignore[import-not-found]

                        im = _PILImage.open(_BytesIO(bytes(png)))
                        resampling = getattr(_PILImage, "Resampling", None)
                        if resampling is not None:
                            resample = getattr(resampling, "LANCZOS", 1)
                        else:
                            # Older Pillow exposes integer constants; 1 is LANCZOS-ish fallback.
                            resample = getattr(_PILImage, "LANCZOS", 1)
                        im2 = im.resize((tgt_w, tgt_h), resample=resample)
                        im2.save(str(filepath), format="PNG")
                        return str(filepath)
                    except Exception as e:
                        # If resizing fails, fall back to writing the original bytes.
                        _ = e
            except Exception:
                pass

            ok = png.writeToFile_atomically_(str(filepath), True)
            return str(filepath) if ok else None
        except Exception:
            return None

    def capture(self, filepath, *, exclude_window_id: int | None = None):
        """
        Capture the primary screen to the given filepath.
        Returns the filepath if successful, None otherwise.
        """
        try:
            with self.lock:
                if exclude_window_id is not None:
                    # Best-effort exclusion: prefer Quartz capture-below-window (keeps overlay visible
                    # but excluded from the image). If Quartz isn't available or fails, fall back to mss.
                    #
                    # If you want strict behavior (fail instead of falling back), set:
                    #   AI_MIME_STRICT_OVERLAY_EXCLUSION=1
                    strict = (os.getenv("AI_MIME_STRICT_OVERLAY_EXCLUSION") or "").strip() in ("1", "true", "yes")

                    saved = self._capture_quartz_below_window(filepath, below_window_id=int(exclude_window_id))
                    if saved:
                        return saved

                    if strict:
                        return None

                    if not self._warned_quartz_failed:
                        self._warned_quartz_failed = True
                        print("Quartz capture-below-window failed; falling back to mss screenshots (overlay may appear).")

                # Capture monitor 1 (primary).
                # Note: sct.shot() saves to a file.
                self.sct.shot(mon=1, output=str(filepath))
                return str(filepath)
        except Exception as e:
            print(f"Screenshot failed: {e}")
            return None
