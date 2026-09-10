# widgets/motion.py
"""The app's only animation: a short fade when a page comes into view.

Motion earns its place by telling you something happened -- switching pages
in a stacked widget is otherwise an instantaneous swap with no sense of
which direction you moved. One 320ms fade, `tokens.DURATION_VIEW`, and
nothing idles or loops.

Everything here is a no-op when the OS asks for reduced motion. That setting
exists for people who get motion sickness or vestibular symptoms from
animation, so it is checked, not assumed -- and Qt has no cross-platform API
for it, which is why the Windows branch drops to the Win32 call.
"""
from __future__ import annotations

import sys

from PySide6 import QtCore, QtWidgets

from styles import tokens

_REDUCED = None


def motion_enabled() -> bool:
    """False when the OS has animations turned off.

    Windows exposes this as SPI_GETCLIENTAREAANIMATION (Settings > Accessibility
    > Visual effects > Animation effects). macOS and Linux have no Qt-visible
    equivalent, so motion stays on there; the result is cached because the
    lookup is a syscall and this is asked on every page change.
    """
    global _REDUCED
    if _REDUCED is not None:
        return not _REDUCED

    _REDUCED = False
    if sys.platform == "win32":
        try:
            import ctypes

            SPI_GETCLIENTAREAANIMATION = 0x1042
            enabled = ctypes.c_bool(True)
            if ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
                _REDUCED = not enabled.value
        except Exception:  # pragma: no cover - cosmetic only
            _REDUCED = False
    return not _REDUCED


def fade_in(widget: QtWidgets.QWidget, duration: int = tokens.DURATION_VIEW):
    """Fade `widget` from transparent to opaque, once.

    The effect is removed when the animation ends rather than left in place:
    a widget that keeps a QGraphicsOpacityEffect forever is composited on
    every repaint for no reason, and a widget may hold only one effect -- a
    lingering one would displace the card shadows underneath it.
    """
    if widget is None or not motion_enabled():
        return

    effect = QtWidgets.QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)

    animation = QtCore.QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QtCore.QEasingCurve.OutCubic)
    animation.finished.connect(lambda: widget.setGraphicsEffect(None))
    animation.start(QtCore.QAbstractAnimation.DeleteWhenStopped)
