# -*- coding: utf-8 -*-
"""
ColorGPX Embedded & Browser Dual-Engine 3D Track Viewer
=======================================================
Generates a self-contained 3D visualization of GPX tracks using the
active QGIS color palette. Features a separated parameter sidebar
and a dedicated Z-value auto-fit scale button.

Features:
- Separated Layout: Left Parameter Control Sidebar + Right 3D Viewport Canvas.
- Z-Value Auto-Fit Scale Button (🏔️ Z值自适应缩放): Automatically computes 
  optimal vertical exaggeration factor based on elevation range.
- Dual 3D Rendering Architecture:
  1. Three.js WebGL Engine (for WebGL-supported engines & external browsers)
  2. Native HTML5 Canvas 3D Perspective Engine (100% fallback for QGIS QtWebKit embedded QWebView)
- QGIS-aware light/dark theme with native Qt window controls.
- Dynamic Vertical Exaggeration slider (1x - 30x) with real-time update.
- Real-time switching between Speed, Elevation, and Slope color rendering.
- Tube/Line thickness adjustment slider.
- Interactive 3D Rotation, Pan, and Zoom (Mouse drag & wheel).
- 3D Grid floor, direction axes, and height scale reference.
- Hover tooltips showing exact point attributes (Time, Speed, Elev, Slope).
- Top bar with "Open in External Browser" popout option.
"""

import os
import json
import tempfile
import base64
import html
import io
import re
from bisect import bisect_left, bisect_right
from qgis.PyQt.QtCore import (QUrl, Qt, QByteArray, QRectF, QDateTime,
                              QCoreApplication, QBuffer, QIODevice, QRect,
                              QSize, QLocale, QEventLoop, QTimer)

def tr(text, disambiguation=None):
    res = QCoreApplication.translate('3DTrackViewer', text, disambiguation)
    if res == text:
        try:
            from .three_d_track_viewer import _zh_translations
        except ImportError:
            try:
                from three_d_track_viewer import _zh_translations
            except ImportError:
                _zh_translations = {}
        locale_name = QgsSettings().value("locale/userLocale", QLocale.system().name())
        if locale_name and str(locale_name).lower().startswith("zh"):
            if text in _zh_translations:
                return _zh_translations[text]
    return res
from qgis.PyQt.QtGui import (QDesktopServices, QPalette, QIcon, QPainter,
                             QPageLayout, QPixmap)
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                                 QLabel, QFrame, QSizePolicy, QComboBox,
                                 QFileDialog, QMessageBox, QInputDialog,
                                 QProgressDialog, QGridLayout, QGroupBox,
                                 QCheckBox, QFormLayout, QSpinBox,
                                 QDoubleSpinBox)
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtPrintSupport import QPrinter
from qgis.core import (QgsCoordinateTransform, QgsCoordinateReferenceSystem,
                       QgsMessageLog, QgsProject, QgsPointXY, QgsSettings,
                       Qgis, QgsVectorLayer)
from .hover_utils import detect_field_and_unit

# QGIS 3 / Qt 5 ships QtWebKit; QGIS 4 / Qt 6 uses QtWebEngine.
HAS_WEBVIEW = False
WEB_BACKEND = None
MAX_TRACK_FEATURES = 100000
MAX_TRACK_SEGMENTS = 100000
MAX_SMOOTHING_NEIGHBORS = 128
MAX_GIF_MEMORY_BYTES = 512 * 1024 * 1024
MAX_GPX_FILE_BYTES = 100 * 1024 * 1024
try:
    from qgis.PyQt.QtWebKitWidgets import QWebView
    from qgis.PyQt.QtWebKit import QWebSettings
    WebViewBase = QWebView
    WEB_BACKEND = "webkit"
    HAS_WEBVIEW = True
except ImportError:
    try:
        from qgis.PyQt.QtWebEngineWidgets import QWebEngineView
        from qgis.PyQt.QtWebEngineCore import QWebEngineSettings
        WebViewBase = QWebEngineView
        WEB_BACKEND = "webengine"
        HAS_WEBVIEW = True
    except ImportError:
        HAS_WEBVIEW = False


if HAS_WEBVIEW:
    class TrackViewerInteractionWebView(WebViewBase):
        """QWebView with a native, distributable rotate/pan overlay."""

        INTERACTION_ICON_SIZE = 28

        def __init__(self, rotation_icon_path, movement_icon_path, parent=None):
            super().__init__(parent)
            self.gpx_drop_handler = None
            self.setAcceptDrops(True)
            self._rotation_icon = QIcon(rotation_icon_path)
            self._movement_icon = QIcon(movement_icon_path)
            self._interaction_icon = None
            self._pending_interaction_icon = None
            self._interaction_press_position = None
            self._interaction_overlay = QLabel(self)
            self._interaction_overlay.setFixedSize(
                self.INTERACTION_ICON_SIZE, self.INTERACTION_ICON_SIZE)
            self._interaction_overlay.setStyleSheet("background: transparent;")
            try:
                self._interaction_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            except AttributeError:
                self._interaction_overlay.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._interaction_overlay.hide()
            self.setMouseTracking(True)

        def evaluate_javascript(self, script, timeout_ms=5000):
            """Evaluate JavaScript across synchronous WebKit and async WebEngine."""
            if WEB_BACKEND == "webkit":
                return self.page().mainFrame().evaluateJavaScript(script)

            result = {"value": None, "finished": False}
            loop = QEventLoop(self)
            timer = QTimer(self)
            timer.setSingleShot(True)

            def finish(value=None):
                if result["finished"]:
                    return
                result["value"] = value
                result["finished"] = True
                loop.quit()

            timer.timeout.connect(finish)
            self.page().runJavaScript(script, finish)
            timer.start(timeout_ms)
            loop.exec()
            timer.stop()
            return result["value"]

        @staticmethod
        def _dropped_gpx_path(event):
            mime_data = event.mimeData()
            if not mime_data or not mime_data.hasUrls():
                return ""
            local_paths = [url.toLocalFile() for url in mime_data.urls()
                           if url.isLocalFile()]
            if len(local_paths) != 1:
                return ""
            path = local_paths[0]
            return path if path.lower().endswith(".gpx") else ""

        def dragEnterEvent(self, event):
            if self._dropped_gpx_path(event) and callable(self.gpx_drop_handler):
                event.acceptProposedAction()
                return
            super().dragEnterEvent(event)

        def dragMoveEvent(self, event):
            if self._dropped_gpx_path(event) and callable(self.gpx_drop_handler):
                event.acceptProposedAction()
                return
            super().dragMoveEvent(event)

        def dropEvent(self, event):
            path = self._dropped_gpx_path(event)
            if path and callable(self.gpx_drop_handler):
                event.acceptProposedAction()
                self.gpx_drop_handler(path)
                return
            super().dropEvent(event)

        @staticmethod
        def _mouse_button(name):
            try:
                return getattr(Qt, name)
            except AttributeError:  # PyQt6 scoped enum, for API completeness
                return getattr(Qt.MouseButton, name)

        def mousePressEvent(self, event):
            self._pending_interaction_icon = None
            self._interaction_press_position = None
            button = event.button()
            is_interaction_button = button in (
                self._mouse_button("LeftButton"), self._mouse_button("RightButton"))
            if is_interaction_button and self._point_is_interactive_control(event.pos()):
                super().mousePressEvent(event)
                return
            if is_interaction_button and not self._point_is_in_viewport(event.pos()):
                super().mousePressEvent(event)
                return
            if button == self._mouse_button("LeftButton"):
                self._pending_interaction_icon = self._rotation_icon
            elif button == self._mouse_button("RightButton"):
                self._pending_interaction_icon = self._movement_icon
            if self._pending_interaction_icon:
                self._interaction_press_position = event.pos()
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if (self._interaction_icon is None and
                    self._pending_interaction_icon is not None and
                    self._interaction_press_position is not None):
                delta_x = abs(event.pos().x() - self._interaction_press_position.x())
                delta_y = abs(event.pos().y() - self._interaction_press_position.y())
                if delta_x + delta_y >= 5:
                    self._interaction_icon = self._pending_interaction_icon
                    self._pending_interaction_icon = None
                    size = self.INTERACTION_ICON_SIZE
                    self._interaction_overlay.setPixmap(
                        self._interaction_icon.pixmap(size, size))
                    self._move_interaction_overlay(event.pos())
                    self._interaction_overlay.show()
                    self._interaction_overlay.raise_()
                    try:
                        self.setCursor(Qt.BlankCursor)
                    except AttributeError:
                        self.setCursor(Qt.CursorShape.BlankCursor)
            if self._interaction_icon:
                if self._point_is_in_viewport(event.pos()):
                    self._move_interaction_overlay(event.pos())
                    self._interaction_overlay.show()
                    self._interaction_overlay.raise_()
                else:
                    self._interaction_overlay.hide()
            super().mouseMoveEvent(event)

        def _move_interaction_overlay(self, position):
            half_size = self.INTERACTION_ICON_SIZE // 2
            self._interaction_overlay.move(
                position.x() - half_size, position.y() - half_size)

        def _point_is_in_viewport(self, position):
            """Test the viewport bounds without DOM hit-testing its overlay children.

            elementFromPoint() alternates between the canvas, legend, gizmo and
            other overlays while dragging.  A rectangle test keeps the native
            interaction icon visible throughout the same viewport gesture.
            """
            script = (
                "(function(){var c=document.getElementById('canvas-container');"
                "if(!c)return false;var r=c.getBoundingClientRect();"
                "return %d>=r.left&&%d<=r.right&&%d>=r.top&&%d<=r.bottom;})()"
                % (position.x(), position.x(), position.y(), position.y())
            )
            try:
                return bool(self.evaluate_javascript(script))
            except Exception:
                # Conservative fallback if the live page bounds cannot be read.
                return False

        def _point_is_interactive_control(self, position):
            """Keep native rotate/pan feedback away from HTML controls."""
            script = (
                "(function(){var e=document.elementFromPoint(%d,%d);"
                "while(e&&e!==document.body){var t=(e.tagName||'').toUpperCase();"
                "if(t==='BUTTON'||t==='INPUT'||t==='SELECT'||t==='OPTION'||"
                "t==='LABEL'||t==='A'||e.id==='sidebar-toggle'||"
                "e.id==='orientation-control'||e.id==='viewport-legend'||"
                "e.id==='time-range-control'||e.id==='tooltip'||"
                "e.id==='interaction-indicator')return true;"
                "e=e.parentNode;}return false;})()"
                % (position.x(), position.y())
            )
            try:
                return bool(self.evaluate_javascript(script))
            except Exception:
                return False

        def mouseReleaseEvent(self, event):
            super().mouseReleaseEvent(event)
            self._interaction_icon = None
            self._pending_interaction_icon = None
            self._interaction_press_position = None
            self._interaction_overlay.hide()
            self.unsetCursor()

        def focusOutEvent(self, event):
            self._interaction_icon = None
            self._pending_interaction_icon = None
            self._interaction_press_position = None
            self._interaction_overlay.hide()
            self.unsetCursor()
            super().focusOutEvent(event)


# Compressed plugin UI template. The standalone publishing UI lives in web/.
_PLUGIN_HTML_B85 = (
    "c-ri}f0rA_aVYw~&hd8`K~Dm}1+W0PORaXft4ooT#6+?fl9Da)@aPVJS?q`a2A&y^3o<-"
    "M$&0V;D7Ks%*>W8FInMQoeVrS}H+F2rmYs9*zDbUjl>YMj2(PR9$Mnop_sn3Il9F7~7BMs3)!o(A)m7D1)n^al>BO11v+-"
    "{2&O!V9XKQ<(dF1SY=Z@IF4*PR<>cy@zJaEIvi+5`;UwgK_RZD%Fxl?bqcFXr}&x0_oIm2KUdoxz{wm*swc1PYVf9SRGq2>59Kla"
    "^8I~uwZZ@1U!Dp;`}PrSX;7o2OMJG|+<!r*z~*>(O}l!@Y{dVoLMVGzX5-"
    "Q)%QX|uLwqu}<o^VohD{_bSI58TLuulOhTHTDi;_O;)Izqzl|MeL2(=QCaSn+F|)BQIp%2V3wr|9uh1!3-"
    "MTgul7(?r_L($G&&FXU+_VhVQbEh7pWIAG)}Oe{x^<+5EKk-KjrWZacNB-"
    "Z=1_moL>?PVGg17zR<WA3LwQ&wD<98@aQn9eJU@PeY3QI}Djm&Ewo>AcQcOv_p5~FQRQ{bAD(nbKv>o12&}o7Ex&K&b&!`%uolY?"
    "R8T{?j=7@Id`2wa0p%UXX9;$PX?Ql!yRYphGU;0zPsbh-O&iYv)@|nK`>foX(@%PTW;7$p|4pPXBx&dXz-"
    "$$DLG(^U>pXE*@)q25`?k>r|oR%R1qMKY=PjG7w%7BQ5^WAkvGeH#?=X^?cTbb`6Oosz8z$~jr?dnaT#j%C!S$W)=xPvcnmvNy}2"
    "8@Z0$Ig*}sf&cp>}6=G8g1o|NuH{>U4+VRjy5|DNjS$Ybiy+Lq5+_aD!Eng<bMMz9(~Z{o)OEzdXy619B!=H`miStweXA39Mm@kg"
    "=_e$CcVL<r<8O6qjpbb9&0nDC9H#a^{Xe&`MPm>5YcrrCKOyK^A>^vgSKMpND);>bWYZPuo{hy!|#)vO)#$pyp-zUNJ;)o@!((3x"
    "E0g+y&b#~X_%_V<@<u^VkW(cER*(7=mtdpYuVC;oWW_G53#*c32WFQlh<5U}~5mYriDO-"
    "X;JySdpEB%pxiSs!OwszBF{{&pOU#}m)6cKRE}wLyH+cqE00vKWvnL+k*&XnJ30uDIw5sRO7B_JGOA>9MV6y`L<!G!Hj&>)|4dpp"
    "ALp6C?pd$MD1cX7Fd38h<*@PLCqu!6X>oH1Op!X1;dDKsC7q%UK?dr0v~eM9wB;7R<a-"
    "J>Nb6R*>zxnBLS>a0#WvtTG|2X>Ys308iX`#5N6@rn~07pj(!pNtT#wm$&@LA51(e$M0GO8;6X#*w)8zQhKm?Hkxcz?tra2CWB_0"
    "jDtDFKqhSKrQVx)Lb4fbb&FVP8BUp@FzZw#GFn+ob@-"
    "NvN<zzCQgR)bn0Z0jK|CucG4aM86U~@tX<W~iwUz3N5x}aOd|I^V9(N}*cYNXwnEZD4A#KXK=#UqMQnJG=2&e9(>IRY7UC$Vq_$_"
    "y`@Y>$UkAu)STz@w4XI`5ab+L4TJSe{wQzJmdvsnoX+882l!e|Ec@A)FW9xvxiWoMje?A30VWbL%ToP(9y3ko@WsY$6OU&viBEm-"
    "ikMv4101N~$U*(-"
    "=DT*B~Y2TXRzs=vV^26`YFpjocSVfi@wZXVcvFkD1wBA6|v123%IpuQn|`DyjNU!fv9r`F=_V1GaIxIRS>sst^wwK2Hq$8C2$_n6"
    "u_8`2XZ$tb@B392q5=;ryAi5~3Z+1{2Th#1;f1MvZyuN6lr7q(du=xr4(i_H=kGDv`x7Ivv6Lea5-"
    "o1N*JwVyL)?T5`{sUd$1tgdh(Y}_bi$2uR(SaU90?6vc5G-"
    "Lo>=fz+a)LPDz{eyclRiE|FciF!T)vSpjlWpC>V!~7e_9Dv4qinow-"
    "1xQ~l#;72hYr{K+EEPl;0jU(?xvPW<zi7zFiLIHQ5<^l@L(kgSRZ??fN_8zbUJs1R@q~J)7DA;xy(*|qDpi%8r2ORm9}KYlANcCc"
    "+XZPlD8}Gm84X1mW!o2D;=PA2HTwCz2&I*_5o|MjcXJZ&+X8iXPX6$Q)r9&Chcr>r;+0^PSwg;my>TMQjKRHuL%GeB;}5WB~4(9?"
    "hwqtB}7}Rmc$@24T)(qB>+`lmSjtY$*F|$F)TDZT|$%3bzt}HdFVw<<K3;r;ersb($oJ~@2MxxjQaFw=+i2_i}~v1ySML7qQd@JC"
    "5}Q?sP}Z_!Cym9g>m51iAG!JK21n;5B=y<ib8YOK1~SBte2LF23qvU=rQPQ_pv9t@Yf_(Z;?lJmd}Y@kg{azdnIY_Dco)ugN*&5J"
    "CTK^{%Dl5>?*-PDpiDlU=tpcE><WW6qk^&$_XUpN|n$R84+d9Z-H8zXWTG-R%^C_3d<TArF-UsFVlCiT=?fo_DR{L{0XuWv--zVa"
    "e)q*$|Dz7BM!KJ(*l=`Zg+p*={}CXfx|fcP5IF6I-B?f-"
    "m`}%H~9lpLJ7WUFotg#ZMZ8U2d%czLo?f=+)9h;W@~OmOK1A5*gQrw5R})N6mOzvgmf!0X_#zY`b?j}9~%aO`*_b#S20k0YAgx`G"
    "n_SLDx;Y{n=#BqbFlKGpxs&#5Yr;kfL1iY6`gR2KH-6b`3%tDt|AEg$t|OblLF6XWmfr|F*F%Uky)Q!bzuv}Kx`G|53Hq7180HG-"
    "3&t`ciSSymOlELxYK!?33^^^5{;^#1LsggE3ukn)VFfhUVZ|~7*adf2XVsnkhg|Sla~up&{Ia*O?QzvrZD-"
    "vY^~(?(mPqMUCM||+X8G8n3B`3z^F7wyVDoYix#|cozJ1$U|N2YF1tCOEpCE*D2{X)9f;VK3Op4JE*|%Yae-lV>{VuQ+-"
    "IUoGrlgx_VVIc-e0R=XjK0@u|M_N!-+qigS0GcQ@P=(=FR{)mxt7GXa3X$RN$&&A1ttY8(SIRj}6_~EjPlI?=qn{tS)DnkC-"
    "{^V?(t1SLtI3LJ;OTGK6Tg>ejeP$$K1$8yhA^oL<**DA%=l>1=k(52?e+PTw4HKQ-"
    "1H`*)_n2`wOUrK~voW3l2bD=WT>V}3n(b+14oA-wD)%N}|A?qXu_`+Cg1<1>NipiL?%C6RpEAc*z4Mj`MqZ{pqJ(|eqjvn{BmH;n"
    "H2<SMIhhI{(VsyjI$m=;=#+SA}pN}Pup`@T0BW$icxWmdsJITdCFa37N*^H{RCpoiXyM7Ehf*i`$GiIr{=NBvXxPznu0cQS0iHsQ"
    "3LJ}4Yb>iBFw2&Y13X9A7)`9^y)Z=q2X_N3+X@}V%PW9anB&atsc**Fed44pzn18q;?c;Gg=E&SW*Z8ghHc&V)>OC)&$&z2BwY?c"
    "xvPqVms2<5}5*A^O$iwBgmjwT|kIVynro;>-f(L##os+6Aj7+2!Zxry=DkttT;$(40BVg}9nae-N>!}}R}Y)Q-"
    "}x!AB`(?%KO+Q>(f&_kxYRrM1F-"
    "+*n0@DNzpAvNQCKpeuDtvMA2D*TE`UxzAi)^Zq3*&5DdHJYUGO>emmQEKgpAH~oUA*bS)40WQpXAD%>WGl~k)SrTG<<8<tCNPUt`"
    "coC9Q*}3@u{c_U_`@UvZk+%kySAgp{=ZvueObRVoo1QvxQL2;+NsDSZ4-"
    "N{YuZ)06h~=Dg}l?|KuC`TB)bJjYa=5ORUFvnR%LOaq`iBEgOBx7&hLBP+=&l72h2Gz0LPt;9LcL35uo7Du*@6`I1CYB*BQF=coB"
    "MxdQ*LYVy7=S$__a5z=vpf0NY;Vz&h$U*I1d$%lLpH(>sJxP$CTGkpp{T3S7o|)B^aS$3$rlenp-mRzc*r?A0uY+1}*GF_RMGz<K"
    "V{vz?TA62h@{6BRb@JciPRI~4lsh>2!w%&o#|aS%*mf1Z^`byi|Rb_BRpI2wDw)QiKVvw%1twnI%|TvJeJ7^3A|M4c37E*`S|2^b"
    "6qh_*F%A5h7}WcgZQM@|5~WWYi<1<Yj|%`kS5!jS-"
    "`?oC!&%+3ff=|Cp{?#SQYXRYjq!PJ?D9*iQ5Wg4F+rmo>Wn}1!xG*4I;2*nKbvX+C|nS(hc8r{xQ{hUpii4cCg%<q#J-"
    "RDp68)<GXIDblOR5@Q5-"
    "4#>x0Zf$24O0y%^)~4ge{JA=<ji$MykgF4Qel9h$ASbp+_5C;d#3GOF$OMWuWE8KTUO2Hfd1+3F%R=W;y{=2Cm*(oU&;B)EGSf>*"
    "_0K)GH0a}dOT*}T9d(wZi<dP`I8ybA&lfOXDo{38JO}7ZU8x!Z_>kX`X#X3k$%hB(+>JHD1Y!n&Rk-"
    "G&maB}eOS!gTdvP^xAgIQj!HUO-"
    "H*c;iEuZvMt+`z+rlU29h1i`<>sN;LpQb_uXX^7f_YNLn@s$9<VTgl6!adC7A^#E%WgB$A&!&Nx|^YX91?*-"
    "C7DW|w+puNBz?+ZmRvT9{0sXl2N{+-Ki1dgzCW3)mP?>4JFUi(OS@QSsiH)z=R-"
    "M{H2Vm4jd?d#a|0@|YnSugt%AdH@uek>JReeQX*K!EN?p2<+Pe*oT#R`i3&DJ$F>NlC=wr-FNmi$c4HGQP`I3F2B<z2Xg-"
    ";d#L}|T$!nCd->G%_nK~;{V!^La{LJ7S;sQgS;u3VVKrCe9Y)g|!1RmJeDo3Yzw5BhRj&SU%gPi=J#c|v5!jCW=;pC0UF*HcGX-"
    "GwAj(aWqPzRby*((RYgcrsPCw+Gzbl{+dD!fL&~^Rb90PZ^2n^urTR%KPd*rqISF=BAf-IekcI=~g&esh~t}mK<d%9a>dV%mgjH4"
    "}SybufTwKEA8G2{G|$lH;NFP!d10}%jae_A^D8ipU>jYR)EY+o8>bMVz9k!Y>vEfb48w>is2*qdIcXhyI*+*%N>8FEEQ(1LWWBUC"
    "0sXTZf+J@pUV3o!AY1uEG-"
    "&o8Z<$;+JqT>r>M=S)R}JA&cC}BuO6Z^D@Zv7C@yq*F(+IROTdT}`rM85{Z2=NSj|716@%4EhN5M=Vhc*_e!Pdywr)OTlblZcr>%"
    "(Zu4<M080~KQ(vT;N6*m%_MZBE7-$W|5JWQ5Th}^-"
    ")>XDq(8Ag?Tzn|Ng(kW9q#?uQq8xdOpv$556%bGatzf3(Ko5no!_L&67)SS^`=#ARbK&m3}+{`wB86=H|tEqFroM@6so*lS&LJYP"
    ")MkWrVz=!8_BsMS9mW58l;zvu6D`pC>md^a+$j+k^S($U&Zs#;{K&#%G+zQ3a5JB>V9_UHJ!tRRRz$fj3oN^F`^D$rm84uyjW&tX"
    "|`P>{Xv|*N<H!LPs6j3qHGbOA_JZx0KgOtytq}=niw!F;(6J}0ORMHV_i~h{%MIwcO<InctHjE<50Wz`jFH_{U<k1^;0R|Fe8+f1"
    "cX5n4WbfWvX<YXz;q>Gw~4hR0A$30d2^qG8xGzWZn4ydp*zy+51Km2bM{@1hfKX)=I<Z=T21d~&zjMkuxr^+O-"
    "v+y_ILL)ai@Q%sgl6-XC>H$NgxMQ;r)BTc|?wou&HqtFYTe8B-r_=2>?cMW-"
    "c?D2oNgIVX9rTU9<2tS7^Q33M*rnHgx4QINl6C66oL3JVSP2`n-fdz$G*J*mtZ5-"
    "Boh7}<HVX1^_PbT|YrEwsyP$Ge4q<hbMI@b3PxqtSNo`Y~^9If}rgeatf0f#!ZhM1vEDEf&K@rp}V=+y3@Ls=?IHmIh<XipC&6bn"
    "=hYfnO`LTP)mk3MiH_HV`7j3S^+ol#NK%kvS+L`jsVbk9x61kQ;HO*tXsH|lbu9}CfAlu05VPeOh8bi=hev9pGTK8BvOQmya+Af7"
    "E$oWUL+bPOIdh(pSLMnOQ&B&XZvNKfj05K&7q0<X1Z7*j4PO;*vUJPE1k=zo|Nj@046X*QZtC{=8L{Yvf?PmkVqX9E}{SV4rkGaf"
    "s9$Gb@(L-"
    "So$!>9`yHl2Vl+w!Rdu%kK`<R&wgvVTu54@?@9=YL7l}t6W5DLVCjc&`4|2I>u@*UaS+%zO&9gR!^G_KcTg?`^Z^hT>|^A&x^bBK"
    "WP^Z#`=j3UTp_hdzoVhcbXk%VggiI@;&7-zp*#=3YW%ZmEVlfDtLz}s0f-ytHObw}Q%S)!&xLXAS}nO-xi6+>j!T|Gc4uW<7fmbI"
    "wGfnKji)ax0oYJw=hV}fvfQ2H6{20fEkX2rV@PnhIwM~oH2?$gLPI8ECW^2|Wrr%qRsg!8QrCqd*LPc0yH@d9Iqb(6MRDGVc^ce9"
    "GPpf2J~h;s8G-HMu#M2P(wDfGl|Ya@~(8rvo>T(iqdHiK2Axk+_|z5=Yu)gKRCu+@ajhjxQcxzr+@s`rB+wlUkTmDyH^H>-"
    "4bfF$b8M&*(?SLLj5Jw@bJR%Oiz9*kOTK~4y}#rEvFyjWg5KiY0P*A7C@>%0-"
    "6_V`jb@Z<9`K)c;m7t#+ye;zwgINYrr#PK}ZUSA)MW^Y6t#ts*w{Rv~(onbItci(Ui*C+lUT8{<X^|0695>Hcq#@^KSSVs_G0eDt"
    "+<W2lrVQ1#W>$CZkgZVF@s%>?;>)xR|olm@IowXvP%GXmv$hLNN9kxIC0n9gzh^z-"
    "fGWwB!%kf9MwT$~g%^6PID9XRzOE8ahGsTzys24F66X(a!fA+OR1t5^x-iN>W(TD%?&-"
    "dT^$NS&=ix1xW``^9)XCMB<FCYBqt4DACWvA20!qT7RZw)6GLU-I|gwY;_!Q6T7^5>Z%GVz>6`lyl#V_-"
    "X&vE>iB25x%;NESyRFnxTf@Rxa=MW0p^P?xDyfKBbBrndM0eedmee)rznBtj4Rd#5ir*D!Vh(#d)uBbI)))O_#$Ti?9@)^89O0@d"
    "IA(g(l(#nG34djIc#aP)&eVF<tf<!?Ut<9|N-(yu>y^DFn?`k%bO+4XrA7|-"
    "Fw1C;=;H5}@AehBew9#30Xb71+;<K0?^eW>BFFe@&Z9|%La;R|yR2h1N7))=d*Il*i=@rO(+9tFe2lrfji*o!YtJox#{^3teLPj{"
    "l;>|ljPv$mHW6Ng@&pk83=sg}A|tW)QzxL7?+GNQ}kTG|v{<YxxsKSd4gw%pp@{crvD!4H1*;ZJ`1;5R>Etblzp;49snxE_ShN%k"
    "Fr-mTq#=NBLT(=R#bkG}gi_rLouY`Wm06ck{{?|WzAZtOfvPNAFyG9VK+;jr=v{HBIZbDGrkwK?*@&h2pyarF1^-"
    "v7xzon7bevMA-"
    "!4|EojJ+}FaEI=$9%rOxSrd(%ZgB(#nE8jc%)!#aetMJ%7yUq(0S68V!+3fw(FYmwgj?=g(UsV7TO@cWifTQnzpZ5YDP1PA?;NS>"
    "SULD6o50U<k{+~a7@K--S`oVig@Bj6KpZ)s&H-1VIW{$4)3wp^}&^d}oiiy+5cFM8jCu|emt@Uc)m#|wq-"
    "36B7&39|v&Sp&_uGi_N2|3?ks`+GqhvxB+E0Cr?eKb6fGZ_qzG%G!tk7Zi<e(>NQ-"
    "+l1w|Mrk46zxmojP?}{dI|#@>Dko#X`jdWL!C%I2y!OrQIwuV{l>|v+oO}ZHH!B%^NP5qOKu5A@4tEU^?zUzz|j|f_R+h4|KSh++"
    "tJ(q<LH-"
    "v`@wr}efY!we)Lzr{ouE6F_wh?{_g#6v*HAkFV#)$4lCZt)Qj?6JbL?QqI2K<`u)HD(ucqK`Talp65BD4{_;n_JH<O=U+oi-"
    "+?`|<d9@%0av5-gfRKH%2C2Lwh18$$0H6bM@ebQbzVXweH{bv8EC2Y>w|+s>X?D}gX7N-"
    "Q<T4PSL=N!7Kl{Ir{_5xVfAsyM_utMew)}U~l1uMi;NQu_`7izW=udxS+4G2+kN}_6{2zaG^pzif^v)k0{p8!#>QCL-"
    "!ksXTedR5C+r*}sl>u!;;g(Tu^k@x|Hu8mW7$9jGm?y;OuriXvxU;3=F<ol%?tkaaqo2Obh$mS>KYjayUwsV({x{!$@a;e04E2NG"
    "{L6!PexwS2^f%vO)mb-?;mdXxGe1U^6qqM9_*KW`9-"
    "79~>l#P(gCC|w^@AVUMumlmQL&Z_M^!Y<sasD}hqxBS+B^F0yAR&~=7+!ibNKz<-&(fH6uc-"
    "7<~2vZJw@U~Tw<2P3j@wa==4sc5x51-(n$Y=8X;ylY2@??G{SFlvox}CB8|YTSQ^=^6_8c#*(eiK+iNp-ObSIk`;xjN)3E72_|?B"
    "Ky^AS_4}Se+pz^o>l!@3!?|tk3kA8gg^*_J=-q(-b{uX1v_%Cd|U;LTcddv4*KkF8HZ0ZeVOGs=3Oj(*q50e+cw9B-"
    "AIU26g9)0y2AN=O4_y78R+|>T<+kg4H_uh8+OQ~*W`^+cEWF07Rvkz$@kg<Ix%Ue2OXO4)V>8pwT3OpRo&F9L?Q8k&fN@=c+3hAk"
    "LC9E}tvnbuAw5i$m<JYd@*=I5(uYqv$r5}Iv<{uyZ>YWGg|IhpH{8kyf9QEO&zxx)`g$*L-DqXn~1k-"
    "E5D?FXz3+~bjwLbA~9DVtxOjJ7h-+#=7An4UQUv>Dm55M#?zRjFqm+<bgbLd-"
    "k9@C0yJ^JWNUuAUp;IH1h|D9hk*_=te*quLd_y_*V))!rSlOAGf=UcJwjxYhKWr{Q;uQJMvj+bRLdW{u0FRnV$MkpA~yTAD8-"
    "Jf$2kS)#c{4r30{_?o^Ci|R(NE`ZmP8P^@7}(-FKRJ5uhfLyp@XoJcR=)PXoXp#Y(&_zRG6`;*di*SY&2{&~Uw-eSZ@<k=i_q6U|"
    "AC{w`nV}b>p9Pl%X)q}O&0&~{?V6y<y`*ka}HaK55DrH+8+MK*C)96RP$GF5zSwH<vHiU_x|RiFTKq=QQLz@YY!-"
    "~(ZGwd*K|qMB4ddV##b_8HDi9|`#E=)5h}E4Bgh1oQ|1;f!bk&vUFIjF`)gE7QfDrsNf3ehG)$vnRs>OKX(Ujo!Bx@@01$EcPa3}"
    ";y!Vv{zy3LA-S@xt!=o?%g2b=5Yb^^B@4>2Zhb_>RHfrL0Up(Yny8q381{p0elAK-Vu&WsGQnz9W<5yRjG0#xeug&Dx;io@8`sbh"
    "BfA_5qfAhb<Zu6_Z{oVUt?*7}Gf59sL>ra38{#P=m5V_*jY{%()v_xOqPs!dsMRfLb(phdg{@`E!;e%iQRCSisX5D>?>TSOKN<u&"
    "MJfFoQIXp~~xKfK%?36-YF{`b)G^Dy>tfkGvkBNeuE}~Y4U4zz|&15_*Vkeo@p{yAKLy$sKu1@3HHyCnr2?8?&-"
    "PP?YidJ;Avki&qL$J60@GXw~TGjTGq=PHA{@~YtasRvDqgyYUkxWWAzG-"
    "9{L7`JgG>p(b`lG+7JjzV!y~;!7g4F&0{`XAyW_lWvd5zQqZDvz@sRblPXeJd!<8<;W0uXtgVA&Wl33;**)5Fe+m-"
    "ZEtx=zPvuO&sJbkE$;sJ8dJ_uo?OD?z7B?Zte=^x)dw{l8+H&QB{<9Wk~IRgb>>*B|}m4=e&+*63}x9LR*uFTeZY_i+m(Ey0<CGp"
    "6l*@ZMV=y!R%nY1tZcm*hw)@7NFe`f$+aJOBlbx3}`N!5tE6{Aw%v;JrU(egELSAFJWl_WtnS-hH#u#K0=Z`4Ql9WO~{+e^Ch*+R"
    "|99zphkk1`*!G;NHp&j`imci%5rKTAcLLge1HHGN%mPPJgqu2mk$l-"
    "&JP}xiVP9Qi4$gZ_^v?^(@5zP|pUaU)$@Ozi!m_Hq2kQYI|GySGgD@9VX1sdW}zQ@1wu@lMjFLeW%Nb*q{+=Xiu%7;ej{2i5^nvm"
    "L>sEiF5SZA3yloKbTLx(p)=xcM_WhhY`p9?|kL{H@>WKGvn!Sv6OFd6;kpEPj8fLGbx8b4c7X)({8t&KX~raRp-LFYv-IxpZ&dy="
    "dWFQ>9fw&Yv-<9gRe<xo{gg8F`mP=iV-"
    "?}U2tR9*>z@%iGJ5xrsVVBqxKTca#UFOQlevC8kVUbyr9Yzz6NKT`BD}{T>R_>j)tW4EjM&xZS+RO{NW#F8-"
    "l6_X>?eSrB(FzoyMbj&0JO~xOV7no~EEh#_>XL#M+t!Yyn+mPwv<Q@3l*@H*M5Tk6y>&ynYKk_+FRx?poG)SayWNYi39CdskojY-"
    "jF<kta)J>fdvQZah3_KuWB8Hv2^JXwjoQ-TYm8bw&rl?bksHgfNMP*RqsSN?UPJ<jy6Ef%lZP7#srX4cDeMTS?Jw?dEj-"
    "z+RBITzkBhFO<EtnvMgyg=vU#tFdUkGNRAS1W)F}`U2j1(d&32)(rByt;0FR>sJD9ROm#v#<f;Dx`xd#Rqw@M;7`2Qp*q%jgc=!A"
    "ewsPhSVz9!!(cLT=MmY1^Wq!PX%Fx7qAKy5zU)1Y1&1OTSH6$D7;EgY{Vx2?(gZ{<;8*;U{R&PV@U`ECzuB)C*8`u=bm4Cfv<T9u"
    "JvP{azq#)`QwB7?34gQS5B%}Lgu9=>dF+`p!=bUr>$0cxIL4um9o@n|*{|T~^196XSi9<t1J8N+5?P8n$UOC#)bynGiF5P$WGQB8"
    "w45=hjHSYx0OQBQAc!5z5jmK6QBre%F~c<iKHifh&SoRdaW&+7>~-"
    "kHi!d7t1k{1xcOB({EPn_$<efaYxFfPiVcia%twFfX<&g1*FkUw5GS5`K<*33WAdG^sfPwD_gbJ78Ab2>283cSs5L7@72f?GwEHJ"
    "PD0wL4qh_UeK7~5#D1RoC>N+SUA7#ABLEWrVhaoQY^qIDZA9@T=dlJa(NuNUE5i<C(Ne%(9?u;6Sk;X4Auf+PrSRoWnFNn07f|62"
    "RM?xw2VtF@fki~cYSqF_IEUUi@Md|cd4<j$gY<c0qJf3<V~7JOmzPzXn(syJ&-eIC~hp-uW4hyOOSOyn81${fh==erXJ^I=87A{^"
    "=r1LZ7ze|qAH)Na<1yHk#d7{e0?-"
    "}!p86NN*@QAp7A=%8mgxq$U9`>w<H{qp@IkC#*r4dAnxKGM4>`pPhYcMiM%^}29iVslv+Zbs^7w~Ify%Z~U7rRz6Rlc)GNLViv-"
    "vz(8jMsA$Y{=|)dU%%+a2c4;V*yy#iE_Ei}Y#bjrZ6}xcjc))>oo9n=OQ)Fz+hs5-0qY+lK@HT@oi}9P8u-5DV1@xwau(ou-"
    "YZ7R2(5wTol}su#97C#H#>3gEF@8A^qWxb@$3y1_nMtI0)N&3gkgzh-IRJoVeY^?ycS#;4;lwV?*M6NA7EHRRf5TvP_Pp%22l+6)"
    "i!#~meV_9z}Q`fNhA#pVQ&x0IZyBeDr5cqmU=zd7VY74N*trRKlKlv_YThk>Y~QJA4Y64B9BqzsO7lRU@?oyL4l&EMvm3kFb4Wd4"
    "kSA+4pOI~ZcF@Tc<-q;O%xO|$S}KD1*0k(ZU-j4G3bPC_Op3P_L@y!ixth2ORDae*B!IE<EnKByzYS29i-}(Ae(5)nDoKb*qb-"
    "J(b!{HfPWYQaQaA`+tAjKQ3MQBa~(nU#FGh^@u~P!<8-"
    "@!DjNo!<~cle^%)h#$~wWA$&>K6o>fn7E;HpuL(gMeiRL+sVItiDwXp8Dz`*+o{_7sHe+e8+Kv?8{R9N#B7&O$SwnPraIlC+1N;x"
    "zk(S+e#@7`$c&=?2sjHgK$>k6=XmrcoHnWj8pBi_J`3lJ7FcGW1wQMon?3~o1RfU4OS9oU})LD=9ek)7BVyAkZ3ygzN;!cM{J*RY"
    "7?NlXrGHQ18r-7s-Lg57V^%b#@^7y!ee97~fhDU7#i27tB4MYRXpGz-AmgH-Lz)>6z7yfNxNv(bwl+Zu)q@q$*X<Q{qv@B11l;ue"
    "*z4;LZi&v+JM_HGap;>|{{h>hv9c<$1mY63Is*G+TdN6%tb=Y|B<be>KCZx{B3#>0To1g`Fj+!MUP>N0)hJkv`dmY!sd!f9OBCBL"
    "xn!sS)8!3W{F$)r*5B+ke6e0GBnRytc$3`Tc{{c~0V6>EvTTU#T=k3>>2izsMG;qb&N>A0iOMYtE>1wV?t8JmDQkF#wlR5UyzER`"
    "TYUwEwRUMF@###lSJAMBVb(|BS?O8kF7Rz)RZpfmNnq-bd+J$1&S_oxr|nhn2cHSH0?@%f26oj<eW#JlU<&Dx`|&=ojOe=!(&wu0"
    "bQ>2X5D@|Lpm&{Iy0WnjCh!rV)hl|yH7V^O%1!J7>|w8x88uT`RCdcN$=r-qyt>~-lm+xzs=1%q83LXwKb8-"
    "8k?Zu(`P)+!*Z3J8nt?oLT{D;ucAs8>qf@JLyoJa-"
    "h#CQHa*D~mag?lQTB2S3%zHZ5X_FhbsqB3RHJu9Y_GyhE}nwzyNRmL8o5?Kx%>_YAt<l<^(zN32u4VYV2{pDv~hUzd1N|0N1+;$2"
    "(5q{JL>4l)Txu-"
    ">jhflI*3TDbn$R)|leyy77@QuP^&!Ig29?>V~c)Jj^c)Jn1^z~^1;{tkaQy9;?__;16mfFtIeVg1Z@PjKuVx*jT2q^wJBsm&cz^P"
    "qxVac?tF8F_(i)Frlkwuf6v6|_tCUx^H=Zy+-y9W@9IrlUIrj;o+-A~gC`v>{%#I9{7O8oesY5U-"
    "Ll_X=7R`fX8@QVq7YsS4dQZg)!XQymaZG=VE?Z#m4S5(qO#wn{q^ecs!ZJu_0V$lH`!xt&e1s@y@8^yHM&?R0z8WXa~*yAoW2k+t"
    "NVb@<9AGKCa%3d0P{wt`hGW>13QO*@xEG@JJS3|;0&TCO4rQYN-"
    "bOWWlD7mnYbv3<nH-8l=8u=<r1gA6LoayCq=KFj3rB=+Ojk^oE-DM>x?ga8CvIf^SJY1^c>++^j%o|F(?TpjROx0AO?WGbOJ#%dr"
    "*b&d?CnhE{zt|8fHS_XaeQBi?Fwh0uv2Uuo0=QiO@^7$X(&r5?hm=*#TX+{m(>@+)UYrg0X4-"
    "(A_bJG~LCFngpQTt+scI6y39eY7DC6G!?71f{uAs99X^n1C2I@j~=Y&3x*!Th@tB&Dywm)X8TFITo{?fc^ecgE4(SFYF5X#<V=7C"
    "VH}JQNtVl{J(vQU`$GSxn|th6kfsQmm4YhC_p?m7ib1$iV`*QjhW|+#xZ@uoin<vBux%F{SmdrFEu7Ky9T^+%%$CwzJFK#S#HfU|"
    "~BcREWyZi-"
    "O55jYJ#nFlHLli~g+Th@WoW5TXi^qM`blJ2Q{~ZPi$FJICBmaN^xtI2p+XB6LB9ubhD)!{)asSqqQNP~=EKMP9>T64HnfOd@9TUc"
    "w=zl1AYTNS-"
    "K8%LiwhuvUrQ=(YJPSv`4TeKF1o_DIRJ#vTC~s2;H?tk@%7J$Xucl%Mbt`uVN2$x{lh>Rhn5f<m5!DWqvLOu*cNvz9_8VI30yVI9"
    "B}k=Nkryj_G_WeDX!@Eu8p;lF?u!iFw@<A`@E%QGh@)tBob#p-96ww!Inj&Me-"
    "b3gWa%hCK8>mU8yw;ufHE%s7)BG!3Exq8m~@I+>pjPOj)6S0nPD0!1zYJY(93hds~PTj$yRD8liOSZV`^_Ba$y2Y5g7T{IPyUz6+"
    "Rx^w+&*RUt&StR<Mi~JG_gr)266Gg&Qgh`X*PRW6QDf?k8KM@rpD4|j@12GkBYzCi!md`8Y1`nA@U+uqxNc<Lv)4WLx=+F5=;_QC"
    "(Ltlm^RmI*rM7Uzad^Xc5-"
    "ptb>jlDN=}G4Okd*lRwHIFiKWyGP?mAI#mOA0#OY|ByVUi0|M6v9eJD1AHRwt<nOW0s!2AG4wdqEu5V61!Uu;vzZSceBc@TCLiPT"
    "-5>j%mFpF>FfPzG$0u`CJ@_K6~^Ubv?lh(^k-44G!6qO2WsK92U^Joi@zK+*C_-"
    ">}k;;unRnXs5i|J^_@inpeMgWK*@_Q{>(fS!uri4{aE*;=9(k*as+wq(2tHsj#}<Lk{B_{+y)>Ql1_rrO3Bo@C1HvH=ueo&0ZkWA"
    "j>gMSuHNLz)SCg4$%@E{Sbs;;aenr!G_5sivVo_{m@FVjPUqu8)|lspSG-"
    "{??(^w&H}F5?2`snNKqKHj_Z+}Dcj!gNR@T>@YX_clo@ZI$hrb>Zv4U`lUge>;&*tQS$y;zd<+%HBOMq*HHN?z4x~pWzx#G=T_JU"
    "s~6SKyK?hx+66=mH+-"
    "Rm?1FAALKwjU1<z?uzplY8nxU~#}Y8ae)S>WzFRO?GHNqtLzmQZkE(l?;9;T&~2FP}{8F2O;E{OBfNjTS6l;D|N*%qy}WrySYz+4"
    "MP6NHY@r`T;M%9FQO(+A7U#jpW)0~SLF=>2^Fll^!YFn2Zmf%uq~T9VX-"
    "q9W5ZB`LO0WACxII`bZX#gYEvjydLeDPMWNyltJTev>BtcrWy2`DCl(L;sEi`}AyGX)UK3+2dDI4POCG0X%}fPTy<;|KBLwa6Q5%"
    "mm$AATA$H|X3`R*Q)=bVXJUd+h;J*}FuCWeyUOBqb0CL%9O?K3VIp%k5BW%+wQ4^IM$y)VN9z%B?zY7tiKM;Qoe53to&qNSM$a2u"
    "`$&g9!xjt&WLpp&9OJXlZLc$*S%<>Ol#s5D1E@r3he0m&OV(8?Rk!`);|8_m6y_ntC~cm=cNy-fTQ1lwN=myk5{xfeLSau1$V!T~"
    "g4MZ8Sj*^h{Khlv+)v)7)#a`7U}x&GWiSXYFUZCpRG7sav=<O=X^J4E%yUB{h40GIDZJi?nk(8Oa#9U>ftY0n+>>YqWZU889}kX3"
    "kqmJHim3M?G_1d-"
    "P$BU@`TrG@BckZYWYykn{E8AU{lW8546$|M2p;t~z0Q3lp8RnZO;^fq_M?p!={!6m2H>C)dn2g&SMfsI^&tScDpd78ByWhM%pQ&X"
    "BeQGjP)xZ**Kp*VcbXRAPmH_WhkfgkSK`z6wW*z^ifB&AMd!~d1$w_7=aFFQ!|oW2|eZ}7VhtpnwS<vB%(qDerFwQ&BFpDu@aad}"
    "2uYdN0M7jyYq%P|f)@i2(oS`5@<F}vRA98Y^#qNKX;Cn-xP%G|ubNK8UD9_*(M>PXpNoJKW7Y~AC|=`&q0q~kyMRPre593Vz&?jq"
    "5ncA`WF)$wA5ns+L1B&mO^b_!u-GCSs=xyfYZ&@8@Xt9aV1DUZSSQ@Xb=Jcwz~P{O!pm-"
    "O`&4GBZsRUSC0$Ki;MSW#iDo%DcoG1DeRuxGGG>Rn>})*s<BMeyWqNPtfPC(nY&#bmBTcnYNnL>pHcU2J6O7rC)rG*Wbm6w>2M!7"
    "Lh~&ID`~@0GIh>&x=r%_2-"
    "2O>hj6*#`D#jwxF#UfPh;c9J|{8V{_r3DN*ib6;#~kxMykYSEUcc_=nkRf1VmU|lrxAP+mTQ$g(oM&nx1CSqUbsTnL%;W<$mS^05"
    "*6!hb!w`B74j5=oGtO`$-SShXmUj(6ysnw~au#;C-3cLOg+RZA4zb_IjCf>GAbl`%c;{cQH2#pbL-"
    "OZetL>)sM*j309afRRCZji|Il0bTXVbN!UaOjng9#w_Qj5C8ejct^xB5{%P@yG$@u?E&?xQm$3V}#Bih9WPpJp`^th+=m-w-tOAd"
    "Ua`3(EW>;)BY;hO>xjxiBzs$ZBWnPf#S+X%T(&nIwh_=sxtUcY?3<G%%c8jr~YqW{63y%{@XWyQKuoGLz1EP{cr#A(I0&W0q~@z1"
    "{lPVEqao$6{)vUd{V~IAqh;<CLP$3?QoNAS&K+jHoL9kQ7B;FUX<=(vM9mF%ou~I#n<#&`d(UsFwlEVS`cvi?3M8IhRbfwr8q_-"
    "BGcT69%@Mu@h4AW;_oj-wPuO<6UIm9!9-smykVEkd&|N6BV!St7YDIBx$2FH{E(HiEO;*G7-"
    "U*8*$(o0wy%5p{*3#hC!fylGL({1u7C{xUYfnMh|ezu{;&}nA|LShMhp?#>yl8}{_!kY5oHu%u<uM4lh{{zd)b#SEFdO$+quY^Ze"
    "N=5bM*$~z+xPk?>Kl;&h-AcY3?~Bp1|Z2eW&e|?stNkwgEG248VuW4}l?1jA7(yV}L|K#Up6+n5G`X2w*tn<4#yFU-"
    "U<850m|jw&7N4xXwIwZh4bn=*LU89HXcmA26|Rd|>H^T_zJY7={Bd@)4`gwdlVWya{G#Z8QcQDeOYt3Hf`?+1!w3iYCm-RySwffT"
    "lZErCtM<YWEZ$W43?S6YT}kCXIYR+Ym&;IvA1aH}b(cZ1RoDOQHx|XT@$bQ(oZ%{;ny;flsgr@gI<hga2wfvbN}Sk<tHpou^HJUe"
    "DcG)lQ2Ju&#<8k{wKEwGzP{_LLEV>vFwWjZda~Xv<_l^eD-Z-lK$u1nC_}EsGJ-"
    "8O&=Rn$#f+(v>PBnX^ojl6=5)DeAGUH+YJqFQR`JXCtb2@Y>$Di%HD5-ZOZu`o$jFNi%ex#I$h8#Sy38*c~p<alJt-"
    "c92XV8(ISz61h|EK0;{nlrRsfabB(#5$^`cFD3hA>mWC$IXKya2FbZ{(64uIbn;?>)x&^fms6Kbb0z?p?eYq9ROY5+LwC%>W-"
    "PKW56qg=bIzJ!nA5_b3%JPYo{pXFDdQ)BGyg_M)<P++Z?c&rcwvHxYKYZ<KKF4-"
    "Ekt5qIP`RzWj6CUF+DUShZgy9ttUU5f$CCTGX8Zn{3c_ly3}Y&I8(*DC26Of;JLu^a-2nDoV0e2T25A;ldv1655-"
    "a&wzC?g7#aD<qqLIUG%Me#=^X>2q(Z+2;g+gm+cCAY4sjG+w((^(D^mkyd!(3$&N!~Yiwk1v?SbfWAbUbQHp&boR64k{0W!Y8oen"
    "%cj`wVjX$%F+H!+apUOmpCkk(IHJ~lu$fk_oN8+3|K?-"
    "3BaZDzY;ofAdTgv}=e{Ub^O=<!QZ6&6m})pnUS4iJmYyd#}uoIAC>>l{So^x&v0MT90AZWbBS46{i1Ym_p%r3$W$eogqwbcCK5lW"
    "oVC4Z%*XDJ085JSrlpo6ZmvpNVNKqI|O$DTb6SR_kliZk%o4aExsmvWw*x>!W2%lyDPQDpQrXkTX(|4IsYTfDeMvlB}P!!Y>)E)9"
    "zcO>6JjK2uJ{;MQ1NVdf{}T29Tb`MnYv_Aix$RW+-)}$r%si04!!aMmy&t1$@4{Q-"
    "&jVH9+?sF?E$kSS8Z~q@pDjc_g2Eh!rSl8Ruk|K_R|fC2Xt;o8e=7PRql>j|(o%_JiC!=$6#Pe0rC1XI;$VZL<(hr6*<Ds^sz8M&"
    "3_U6tT})X^JSx*IjWVOhhXI4Md!6r?M<mIhcme&@<lHpJnFgL^f!CAeY&nKT~e`nIz);fs<8%(E8D68iTkqjMW{DO7PU`OW|s6DF"
    "B~naQIg-"
    "+gX&wyMeDH8U9SvoU#gJn#d~%O{=JwKM8Hxj!7HGN7Y4eO0iR<Y4(_A8r<?qOki3#%n}yUD`;`EtTo68x(aa<LQfTB0_Q-"
    "W(zg<09O;DoqJ%M9aHHN)Vc|@c9UD$sWf`^X>vb~AC<ejF!ocF50ox<v`{yU%hRZv;^O@B)iNjPvYG{mcs(r}Dzs!GOastJK@!so"
    "vX?)E9@vE@&-}(FIs=V0O2%pOVlmGztj+Wd1Q&*LiPh*j~r<Lt6C8D5ho>%5wVaKwZl5GYvJlu66e#ck^j^9$Yscp7J-"
    "J>n8D9C29PC4KKdy#i)iUYq&cGP<-"
    ";Gc``4`T{o$0mk!7EWF(OwRQ@3}KNw*5<qlu6R(<F>!SQDCtCO3mAF?sFu@Pod$C#nFwiRwJ@@k23uwp#EMG-"
    "H)v~^+1gK0ly1xKTk=TdcJ_kJ``DIgBl({UNhioGGtuT$6emR5_PH_4bo!J>$*!lnDR&<3TBv907e_rOxMHgMl36$j?O(M*gZ%0R"
    "OG^=D$;it_MF`3IVZJ=`_kBnb0Wp}E{6T0J+dPGYe0_jYedKWWAN-"
    "~|p8^;SgE<~!jU3M%9yrRK#gEv_;Q?My8wc#S;|`)=vWS&$2|w}g<Zl~D@{$wYih81Qn=>O!&H>wJ^n#Q>8s>XVTPh=?TN=Uk_x("
    "u>Rz;P66<*se=fVPbtj@c(x0@X_Q$w<a&hDlLp1-"
    "Xt0(D7s0vyP|dslW_gVc5g$<t05Db;A#xJzA~!~I$fXx%ZMFl9_@$?5YhoIS7g&Jt(Mc|#%sTidlUAF%@&&zLiPl$sZw@L0`y3Qd"
    "UFb6n3iIyF?s7Rq#_gF}XX^3_Ne2b~<zK_Q`v(w+wbnc65Za%qLR%%G>xZQwX_TI@Hl#=Qozo&v@!7FgBdhpAdxS3M*8lw}e)lw{"
    "?u8Jz(dy61qZ&?bO1Phs=<w(pfZ?SI5vDc$wlfjvQ9)@20i=NzYKb)C=&ETNncQ>}yy3rb%q#^#h*DXmA0ei?;mkRERYQ}j$H4xi"
    "ek<jY-(q(s}&fvk*~n>5Q)vvmzgEUu}cpW^)_L1B8xGV8)i95gDd3Q<d^eXDmoDkT}}|4eoA|4i8}#BkC|>>S7-"
    "Gt;fFiKh8lQbM`P`o~c|k5Wu3Tv97FZSX8AaHU(-eXgX-T&XqnnPV?1)^T!KS$%NLK5#kjwkjTNV!U*ZoOK*i#;L8c4-"
    "HN12C)}TVT8UUl?mR*mO2Ozsq}M36~uV?QtIv}DdVStE5%kpCTgGfJOs|11%jawRb1#yo0rd-"
    "h573w@R}S%_JU98u`EZJ5k&K)HO;M53*xq{IMt;TbELsgd?d1;@fa*ZMz*BW0J^?#$CA901nroizL&b$<c-"
    "C2elDwgHb#EPGi~H(u;dx*3VR4LLbTV&iB!D+!xG#Mkk)3mZJH~;B#RrCutBraPqEuBIuq)hG4e=Fj>@oq#*+6?wraPnS5789N(<"
    ">{4Rmb<hU+hb1qFO3gKnbL9cK`F?oG?}lZM=O_l&7dixBq)AnA>)`1yumJ0r06hK{wp6UN#bCak^ju&`Fn3z~37)zuuW%T2=7D7z"
    "VH#aj;LipAs9Gz^*V=fj&-"
    "EazLe0ll=J0cXgAa8gH=DT)i`%D4iwr#+rU`!s)%(4tm=R*EEsgvz4>xvo{dA??y2{c3n>8p(z{Gt?lK)4plbZVBl|Ba0IE*I+Yp"
    "o?mpso1Q5j$drGyO{|(uQMPWsD@B)2E!S4&GcqFZRObDh#{p%k&E1s+?`@H1$*}cN4;kBtJu&b`9RyR7ba_qgrt(?NpRyRLJ8LwL"
    "X*6;s;OxfA^st;QIem{OUTxve_{L6OYN7R$UswS?6A)RM1$_$dYgkW`x~@6($LnTjGA_P;^u?cj^v+L?-"
    "us~g_cS!rJ?99)`L{RU!S~`!0Jz}`5c-~NzvYmAyIO;*p8q}!rgfr>%f6jf;a-"
    "B!3;9#&Z7;Q_AM;GZ%umV9FGnAI12ibEC73CBcBr!()(vkqI)C6#Mh*UMg$ou8T{%&IAESgV@3|)kfX!Gpxd|;rP1NbHx+;xH%z5"
    "{OkB2e4>bmfgKT(rya2!~eKQBNs0scv8)V`q^Q7q<3Nb0|zaLF~nBmec6l~|;4OA!cTn*#0yzkC17M}PLKkKX<H(GR})yZ66RH@Z"
    "LRgwK0xyoFe0L`adGf#nd~4=rigHT8(elwF*fjPR*2FOO8e9?tTh^j)$g!V4qahP)l`5qeoXTlpl`k05I4TvA)>8h-"
    "}Ui3VIw*%MDZQlOUY?xreUC!n-@H_}~7%`qgsm8no4O#=sObhhU<NGkDr3d+-uRPbL>H-T#qa_1zPw-"
    "PM2ey3YE&DD7bZ!k}l6b$lrX1bH%0?e?Z3(46|g(w6<HkYFiDUsCmUPM!E-9DNB-fV&erqkWbbBd>l)RQEDZDNQk+)$WsfdriHWM"
    "#ceuKv;A{K<zu`95;s6n20!KltH)udm|#<Hn1qs9~Kppsf$y`|<tneC7T(zMKfu>)0#PjO!F==&VWotKbuoji>(1zl(tCwxON%|8"
    "&9BMEL9DyPvYa=xX7NB}1vR3I*xElZ&n<^^AuVV{OCu!-"
    "<1kEf)49<6(EmtW;OLHclFPB6^36H#%^gAwSaLw`YZHJ9Z;^p_RDdEpnoO1USPyTK;IkmgGy}z>m+%3M0090m{&Y9F_ilEH2lAOy"
    "M0`OQ`0BVpnOESoL)Ad3WY)g`~~G$k}<RVEq;S)u5{av*&KP{sc%!8Lmh#oQD_D|44=e;DWX|bZ60IF$8ziVKANJ1tl1d%pF)AZ{"
    "){74X!_GF`|jVc^R^|eI^TSF4mai$4oHX?^KH~Ct2SKCmpC35PmG)LLEkY{5cPzk2M;+`U<JS1iJZUuX_+=7QQVzXt|Jc<Lb4B_7"
    ";-)qmR-zuSQy!Y;xlaSLqstxb(u1#netC!`-"
    "zIQNFYx^8L}GCaUadr334Y15gG%yb<u2z>Cjl9u9cra01~4t2M%?0vOtP5tSgwNYVrBBX*w`exahe?3K==!4F58)nhAoDT;&H4;;"
    "j`IdG2;T*-"
    "rn77e(Ji2tgGcuP#ZIFzVjRPZrUG8i*5SS=Q<RkJcYgd_~>pQ02DgR;pNjN+3qw$nZ#C}<huQu2b=31n)ZvU#8=T`{Cra+7U?r$}"
    "ZJPBiPoO2V5d|4oTxH<e_Z74l1zGDhTUh)6L3R2|>@GoCcsmVM$>F89RFiVAtA7qmXk#PR1YJuCVEg8Yby^&#BP7sdW?n<03x@F$"
    "}r<c-"
    "bZ*bAl}+&wo7Zs&x1wo}BcOOUnsw#yX)INpW&gD|)qd0{<rW?;xm?l<74GZ`01nEdkxi`mTxZ~SJRxR$(XFpl$zANh=`q!$D*!`@"
    "T5%$LG8h&LrDaMy2~90`76sX)9>ItfU#(AZMOYt#t7!?_=6cd;&l93aUNY1glN$I3VI$)!X4WVq<yKT8S@lo4qEbjrWTP&DgiR6V"
    "T+ea)p*a7~+hi>$Unh7kHX`y^`!<rq>b2Sg?c*ARr;HizIo{8;x-S#lA69VI__uXem%E_Z={Qpri|@Dt-"
    "q$;gI(#}xEwOSw{wY(BYkEUVzbeBMC%X<x5~iyL$_=fiX7JC-"
    "Y7IiKjv^MBmzA7w@sdbgA;a*PE_haa_jC&5iXZF+cpJdrNS8>LoV=MRf;S4<RiVti=DeY?W8ngp#soFqdJd8+8hOJcJ4ZKq}Cs@!"
    "o^m8c_m{cs*jWe={+5a=W6T#l7WX^axBEo(oKmsYi$A-uTi-"
    "|@&umZ8yPqMY_4ad}U7NBlgiuEbJ~`uxM%uBA0CNO#D+4h0N+(Le|q;bxj>$tJWGOfgpjaHE4U-"
    "E|nG&u?Fv?ZcHo%NAQ6pWkjLkN)h{)W=uTA74v-d@cEyGpdTH;h~rJ-xlLA1~2f=!jM0|s|K&VYzY9J!(Q^U-"
    "`r6Z*_d|K;@4i9Di(F)RjM1WDiF&A+A`gZSE+6oD;9O*HL4r0DG+xOXm`@xc#Z0YvEpaS1wQa)A`@}?qiyj*5vRN(4ym9ey0a}P@"
    "m_&RN@iw2n5MOkPO_QEFzVXWTqJszD3D1TM&6RZHjUBM#L-?-a?4>{7jN4l*oko6=Ovt{9a#xQT1=9yzW}$^REcbXKAE-"
    "y8c}EOpLIV=Te~ixWe{(r6h5|CuM>J~7h(*j?!d)v*XkuM&dG%vh};JjM99Q$ySX{LPG&~3#Qdr3IFwQQQN>?u`Gz0%v`7P^xE{V"
    "yfAEzr-"
    "T%oyWy9Q>WzJ@EW`Ej|md2Mt19<{qx8)jlET!<7(pqI*P*TdDX_pd(^ur@75g1cu6t*U9CRylw<5l1kB%G__JJ=!{O43}Ym0~*dO"
    "{}Y+qEah%Gzj^_%MU-BIP%%WWFn%Uosk>6B0X5dy(k&T9Vu1erKQ5>!nu9m4-"
    "eEvk%H(!8X4iwIrnA}geJ|y;0>O>VSOCBBS;GF%>6l@_+=7Bp2Sg_+!)Z=2+ztddzg>haO*aC9G?_5-"
    "hZ9S^+AVLA2TG{u7Th*iz}rIq7HIgKfCZqw_$tp45!CvJnP+RSd*8%aLD(bjHxOUxJ<9YbZsRl6xZGNCzHb5JKPTmisp`j;AJ}UI"
    "yD+9$XcrsLf(0Z)`o7jwSmy#+0bEWX+e6lCH~Jkp%Jj8X8Qu<h~2I36GrS-"
    ">Fz!8M5+zrX0^cRu+Kr7{>&Om!Rrk1o&jEtt)dNT<Io#S&5R+c@<}}eCw)$)2!|1p)<vA0`C+}}9Y;I?r%EamV8~<U&=yZ8-"
    "6+12lru}YxeP`7aE5<rI`u|AbpnniVSABmQY79_qy9IXcy1^PW$PG|kJ&L`iBb<2Gl7>zmFrjY^gau2vxZT|W<N=%2`JLeO~szm>"
    "v#XdAnl$tDCN<T!^!ncczeR=iZeD;8C^*_z@fEZrq~S96WOd5Yh*|PpmifVy!;xrQX46}7|zg4f({W<u!>h}Svl&YU7^amLmktfq"
    "=aK9*vWjnME+VLDbf<36p(kTxiFigpR47P0CGXlkO5GLLM1_gP-"
    "g)ochRdSlbsN6D`J=(VSo#Dqh?lCu4I!;U9E000&(HD$%D>|i=)8WY?^Y;sNt;b7GE2Ks=PrV(Bh|vMNeof#F`f*M)=b$X0EK7&l"
    ";H!A4pt;<;R>NRnc1InrY%XM#C!G<*H4W17OBkiIX6bU`u*tbFm?Fqk=LwAmv_~#SOhjI3mVryUmP|g|hx<>lLK{C4<Y%E@(}2F4"
    "e+j?B0}Vw=#2XDSNI?LJ$!GwF%yeUM2~}eoO?TnaY(mrN||-"
    "el%jyrj808&#k+H3_RA^x7GWPH1~FPQTE=a8E!XhJm<N;dlF<9l^Yi6WL$+#KV3Z)@xPLtJ7AE2qk_np`-k4dnFo;%odhA)pMi-J"
    "O_i*rd4RUc95{9JO~^LOmJ>mMxym=<__1^QfFa_RuzCK$bC<4)NH8Y|bHJFigb~O*7HpIWBM=c}c^r$-"
    "u~wdTPa+AiJsgidvZx$Fda}ZCl^ZT|0P-"
    "S{3L@n}gO<oqb<1VGGIm|wwA3yJg?RKV$2G?h2D%kCfNmv|^h%3)RgPaR6S=IR4YB}!6}76oC{$j<D!XG=^+c+a?)?~>BE^#{?1^"
    "MpaIv8XRt_vgFT8?`QK|dH%Y36PxXI{vF(sGajLRL0AU1#K0>|MFQlwSDPs*QD*$oLMsTu(djcmkxDWJ|4ukb5*eF6e_5ik&fh)Q"
    "18%hx;sES+1HJBo2*{O1|((7VGg?WHZ`1>oago`wEu7CKz~TWEeq{=nVX@_XzwVBD5%zq9gpF!CM^FxduP70`u~jojP5#rvJ23_^"
    "=HV<;T58mT5BEnnYxI#qrMsThH%obCMkFo5_7DnqKB3d{B}6PJ67+yBUvKw@_Iybl*)6o8GXJ_}}Ey-"
    "4&kN>4bmFqZN&o12zqMPyX$-"
    "C|xsfG)>FN=$>a0Ie_+g|lE>UvaP)9^j5zavIF!UY>%JRf^4P_e|KQQx?D8y>ZfHUJ+x}==OxiTCuN8vZ+GGtB_{J9u3{u(3^a$o"
    "ia-e&k5I;Yg}On@mS(K6TJu2bwlX<D>%AY{*&%BD$XU-"
    "H4dRn@P(PMIF&%`%H=SayJNE&B737H2FkoT31vcz2~s4)ap<#0P~Ae`g}9iAZQ^WL*`7S@)L~o21j)!X>lVnGGYbXi{(E0Pdiz_y"
    "d;i-"
    "<@4txvlNVq7S)G)I^_gDf1S+~U7S8x0&VjziRvli5QYy1Ce^!}8`<T17TQBE|{|ai<FL+}%jK*#7bZ9vT9=NEqz$YCXAzIQSqQ$Q"
    "kMjJ~+n+_%jNckz+Ts?v2;8bV`6mb<En8G?wv;%x^%O@^@p9M%xu)oiUX17*fOYrHZ9D;Ng6RlC1qp#Uqt3O`DZng0<z``GE_1gM"
    "gesk=+5%{x4{(1%tN!$;+eikfMZYX(#<BxW0JkLR!BdjecvF051c5B_5v*iCA_IGPN_SA=`JvKJz@>>1udK%&$X-"
    "Jug#AbIzZl3x_Z+`FSSAWY^kT>ye@wn;wM}PON2S0iX;@2j@+}o}nefRrNCKow?E<0>kFk_1l)C^q8FSA$leN*Bt2`?JDbMN_hI%"
    "zbpL)S(1XMJxnx&g;>`U?>B8`uuqgVCt3!SA(tQ{03>wS;bJ_Q()lUgUs3&TfhyhN~LH!6ho$VhupA#42cuSItuhV|`=CbO*_pb$"
    "v1N^qkDbC}>RN?2mnPl~1bf?IQ6$&9skg=t1f|d);HN`zCn&E^nE}9(4z(Q|E#j08Bn?zz5jl0nbx^G%|>e9NCht(U1C&SahfQz3"
    "%!3TTzU5cWWD`Yv^*_<~@S){1^Mqh??eO92-p_cBq>tal+FVtU|qwma|NGlx;|s)_&6D5MDnECgYn(tJ~u+Sj?xv$lI+=+@%-"
    "Rl217N-5TeYwfsV^@$8h0@TR*po~RO{)ph(FvLQM8izR!}WO$(N0H~jZtI-@}%)7PzX~q)xP<CsZTQw(SxM%-"
    ">cOf7(K48*A2NHGx;-"
    "feWZhG4LAY?lgXJ&L;ayolmoVMO(7Bmz9(E9<CO281q3iZX~ZPBkYUF1}_{*%eG3@&M~&sd!tYEK&~{Dz^Pmi<|Tlg48-"
    "2W_r0@G(7YY>9Q7SsYB6NHWSG;VJR7?@s**8%V?;v?DL{_W>1R&B*NnkTTwb|5w_hpY!ibgY*_{h&VX(ql-zpy+^@62`isFWZMDc"
    "d(dAb{s^b-"
    "`?Y(Bn@PHIYR86#Z9Dbd!)++iIczQYk7aA*&Eo_9fIsgQsRSrF4Bd%G`I8({_mUWUCBZ00fiHy9e)NSfZt&io;{UAiF7v15TGHoJ"
    "$<N~af3i{jFPn^Bl|eYXE9<*heTP3T(~sB@^(xl^MOwGKkS)zdbl8fPt>}(*;%IM}!|l!fT7UBtgK-MF4GB*V*P<nV=;6a1{?KO+"
    "O~Xa9O9rN$0khP>EOjtT6BtfP8Vr1)!x1kMSonf)4lLei_T3?_hU@v-cVQN`yRFC{&$fFlT>MO|di!Vf)-"
    "VW}@)2!czus%L;eR(;?3ea~|J#NiH}16xV7C+Czx-"
    "Prcw2yI!7uGcTm0fpTN>zPJOA~sW#A{kWT%sVOTpGU|CO(rFq4cT^~>HsKhyb-zW)mvIogjDwu)hEo&VK0Xv}Co+Ih^U;pfCwATH"
    "v9CH9(JrSPFMV|$l7xd0Tsi}a0eq1S@WPvHyb|BeB~oNZ8Xw2QyRm@+CEYhJJf!99j-"
    "p|yr=ak2mOZ!~wvm{bIaec097>4e^BG1OdCBc@@_lNQ+H+MHM0x!3A8bAooBwE^CC1@`<&3<>iy$Ww>dX^ic)yPa;gw?l&@6a1+B"
    "KG?(dK=zc}unw5)80}s^?nL7`JJVxY!{u(*AoS~#C=i{RLwv`e4-"
    "eON@plJQnBh`BE!mTt0VSe}f=qO2>5{9c0n;v|8;4XH`1qyLFb^inF$C`No-"
    ")CT|3aB6Ey<$EM!s(cb9d;+OuXuLp6Xk~Jn^M1M9E&K50ameG8smPhdcb|a(B2aMP?OdEj@ZlWfI+x>QesGa*gX!DJ6<)X=Oz89x"
    "b9HY-D5>CF^7qrk6sH&3RgobBrgYRHq>MSVDi&^*Ws4cCWY9%qmrSDWUv{V&zn=UUx&+KQ;<<tt@?RbnR-"
    "$njng_PEbi7xO!`B2syrm-!H4UOmSL{$^ifNia1`ChwEnJTHWrTLxLmiLAQxOFXeE9ADdFGF_jWZ4*?uZuVcKFO7-a(^-"
    "2!^Y15YmSWNkw<lBxhR379$D%63xHrJoY`x{k;4zQ-"
    "mmnu<5E{Xao9`U-}O5Jyc#nU!7<4u5R3v-jFC6RqqF39p-%MsQreMD;F<h!KQJ-WwCd4GlM^u^ieMK2Ehp&E2y$0e*1eOE<`x~~<"
    "ySHj=_QPmmQR<<+URExbGO26QgD&svvFA64bub4bL(Z(S`<t|)K8q|OR5^^^)ACd*#!yPJ~4mciP0OuDWB!Yd0y`b-"
    "<^If*hW5#L>)w$rtZij6$Y$jiaZ_X*F+u0&d20U;IQT5lIUY95~4*d~YeOrzj#<zp;rg+i}@K&oZggUilD?9A*tk&on?*{ad35M+"
    "B+Ah8^uDr3UFqmV#jd2SR*Gk$m8EwadGGGd!3@uPpA0)J~5zNQ}rW6#IB>fZw&o}gqdhB+D;FJHb#1C)=lKL=^HS!;@hC$dX?#j^"
    "Am7%FCBU4vK#;%Z2sv?PkH=+o(a5pHtb=NYBTJohNm&%=9J6W=KUWfT@o)UY=#Y2~oVLQo|WXx_=rq#aQYQKD|#zT)m*};nSSYjM"
    "y2a<(I_EF8W$kF6(GktP<&>Fbg1NMvk9LT&++j4qwcDH#hA8zJqFa)9Ix;79$n(O_0g~EH%xIJfWTBo(a$&n=6`KCrf43WFR-ho@"
    "$c!0$_q?FQfiZ?k;hPaXgof9MUwwhJI<0&wBNU{DiU4;F&6QU{%zsBm<0M04RJv^U=!uu=$iHEWNU;`&lJMc=b`jGF$>I|d}>5Oo"
    "+zU7{5QR_uFx|s$uWpB!Xz~%Z}sxSDeLQl$_me<#k5d*)KPjLJk?ba5N7hYxBm-o`_<;bh$I6L;hw9$x<G>6BJrVU4Y#F~3)H-"
    "xU)lOSENmn|qMg_kWn=&ypq)Qqpy+xzawtBgy9%r%9Z^*)JMh1oTQC{Z$tWr4{As1DUscB#rrxb``?eZb0#SDLE6xmh$PhKk)SYJ"
    "$$f>Y?$?`X>)O`FUqo9e3&j<jOD%UD_Y?uR;k<jAeKhO3!T*I|v;*9^GV?`Ttpfxeyqxe<%wl(;^XdlB3qw5*`TSV4q%e4ES*jsB"
    "vC!40v%2C?RVYt61uoIyMkJdo4qYI<Icl93Vgwoi<I<dIqH^umei@{*p!LCqQq?z9$BjTO`e_AzFtdM4iv6wJwUnIEU_hw}wV2Qh"
    "y+QpwJ28oKl0!`1WOlpR{)$rElY@Kh*@~nqV>;KYciz!0A!MngQE9Tc_S^7(kH9Ztdl3&$hQ}Pn!(141Er(K4T2$AdctT>+82~-"
    "|pN#-"
    "3h|+dcWK4uCo#~USwOEpi^b#ORRzJXu!0hb@~2y3>hqy&ji5UovuScaOjFge;n|k*+XXZ+~v<Zr!P1d$mf8)_3S$TdUoVR!@Y|mK"
    "gK)}Tu+hLXpO-i*afw6uyiDxv+G#J&?X~-"
    "Xs^DeZw<PUntwKV(Ns7QNGz@VEFdxA%573v;=7OQrg(UgMtIG+Fsa`ZjC*_{{X8E8AtO$R2YxQ}MQ4ZVWqXV-"
    "d50jBC2#kK62z$#oWP(-B69|C0)6IG!ZcJz*yzN03+J8DkWmG+E?I1e4y5_ZTACM-"
    "9fqY?;y{CK!OsShNpSmuA3>;NQGoJ3QP^@vt?Aw|8Q06fwMQIY64*;MLt1;uf=(O+lh~h&2pa+26zn9=qzOpHSFa@t!0J3M5(^6#"
    "OPN`C{rVGU_iFVU>tiieV0hp{xCDe;T0Da4Pt>>TPq@?h4%1lA!tY58KlkA07=PB_=NA?MJk;tn_VC#0r*`W1uB#SqRCksi1~YIy"
    "cs^kN#ciu0@>%dary!2DMBE7febXlsAEyo+NUz+qF<%~ui18Cy%P<q74<}H&Y@(Hib=?@xlk}KZ*^gOS-m_D?PPcQqyRl_XlADZC"
    ")rJ!;tiCR4JKOw)-JdDladB@(*-"
    "lT_BfZzBJ7=gQB9MBHtUW)_Yxg>5x=)?n*f1pl%M6*fEge$C$mt%C)v@tRbxRlYl9ezDj`el(H3Nu~3*I>N(4{PkqksKr9n)H8UX"
    "Q#n`TqXber7TnaRekSWf3A;%D$E@W#1cGO6FO%^u!a+qp7wP*73M4c4rMi0{q40OE3{z-"
    "6jv+FtqV#t`SQcZqf*xCUMYPDk|jirBZqCvr})%D92daJOIaqQTjx)g&`R&2k%Frv$2a*)tTd4sCL7gjG8D7@zVYJ4LeiA)~rk|8"
    "++sdGkscJaRJXxn?C|dabS$=;EPI#N+)3}K#&OE%4U8iofTLQiQ;{-"
    "JSQvJg2IacL8tf_rHZP`JLb8~=lf5Zv?u*f&w3t?>`gHgiVn~}BsJ4&SbSd3vTbEk&ORb_+khK9{uMWsdbhEs4J*wUy_DzfNJ%zn"
    "UVWBFz|^~7V&<>5({j#^VHrcuAlMS}qrd98;Qzr^3!18-Ir;g;U=|-Vnrl7!GY+bW-yBkA)(bz+cvU1yZ|55FXN%Y~eiqjzH%i<}"
    "AmmPz2TLp_xCWUzaFnrLQR?WPzhOjj_Z}JVHT9$)LD9k{i17dfx|jGpRGsJ`xcxe=YT)V;Fy*Dh2A~}4!o9QcXz%{Jzxe3gpa1Ut"
    "Z!-mXgl_<!nw*@#18nnB>#TCl&~p8{CdIX6pxlj?b6p5YEg4Y9-"
    "{Cgjk^yu#Zd5!QyXk?307b5MO=(ac)zQS71U$j;Slt^1_jTSP<|ey4F))THN{bL&25{1y0rF6tA*+jQI3@`m>FYYwPD>jZdEQ%^+"
    "<<rskd*^=GD8Yaf@I_w)%D&DAc-vPAXpWa)>A7!MO3PXMO0;SsKkDWSe&OZs5J~`<HY!bIz7{>`RlW2?ZNJ0MXADz#>L0&^cH8(z"
    "!?#775p0K9u1M?1=9mUJ{EOrkcpQVHDTOR5|_0iy;xE(<q>tj&<yN|;&9}}9zD@p=tH+j?|=aG82ysfFsA(ZYcIY4i-"
    "LjNp^N394<^CzW}P(W?u&a3Fx$==1~u|jIWk8Myd#E$HP=*={E&ku&)<`IO0c^!q5hd>wzW1{yG^$Toys=%Jon;Ew<lg=gXxj9>F"
    "Pv-X+(-"
    "{tI0ppax8g>(sg+c^s01{q12KwCTf6Gg9A4#b029zN``Xj7*LwzZQ7FLi6Ufd_>iqudBdP6N0`S{sq)vMr*YBthj`T`&+U>l`m}7"
    "6=XmiB=U}bePQR7Kvom?%M;#enu7j)}{qJuaz5nKiKl$y0-"
    "~8mG@BYpG@BRx<;qcBcKK!R&re0GZiJTMs29%c8Wb?#oeli)+aQmU}vr74Mmb%Ld*?Z9iS6$jK`g!T<opOv;Z%v-"
    "jh4(pCx^!yg`<yDZlB6M!SH<kGpJ#WWY4*#Kg&?A?W%G^FVbsL@mrR}T4D#DS53;RAojW~XyIE&Drj2Rtb|w&g0iwSW2o&~vOnXx"
    "_&`goTLC;)iFCmd;idxK-"
    "`j(dZ=1S(4`o*Tdlnb~3l5V<dcN|wur`uK#sNDT*%i)(dBzKM(ovyp5H(O5944X#{MNpt;%9D@`qAi=WTtN0r5XZr^&2K!?qI|$z"
    "l_z3?;HZ!vPYwdM%6K*%wlK#2%!@MDhvA_UzcIp=Hc4x!jaBN|`@=Y(7_voZFgxfxID<H-"
    "M!i3xYd!DB=R(irX;nav@rPctjc(`OXe@tXh#34##S$wZaDFSJQpjt8`0-lmDi2FRRHeMp^S<Shp(Fu-DncD6xsWaVr3Kpy2L7-"
    "Sn~sxWWeeIk6~kY)%>qY3+M5Tr8}0rn5JUSd$SSbz^b55R3<10}yUb$)8;qs04S~l68nSL;erH3fO`YMg&EXC9kBEiTa{L1iPQTv"
    "4k~W5X=qpf+4|kxO={HLQ*E~%6aQzKlG5uy~U`CAwYhlk>Hc_!{uv+lYbe?#^aJLnt<ZrO%+N`s%nj1^@bLqo0G@v?v0HvtYWq(k"
    "gWhI*lN+0FFN}0rSMd|5fN>2>w+O7p+8*9uN3g&E%-Ig;TXLtn5bI>_t9d-d<qa{8s@p(xqO)!VBJB=Xe(+IBeWR=*j<qnyu3X7-"
    "D2{7#g>suRqhfc{KV6~ht+u$;EO8y{o9+`v^@U2%+v11C}XbCeTJ3~SJ0^XF-fm$(+tL1!QiLaLO)sp7c=^=eVjOYt$Kwl8!`NFb"
    "<ZSla5uX%!z;QFN3WeZIyE$n8lU$`)R9s>odFU3+jlAWs6Xj+NTg5=$~+O&Lx)C9DIE?gp>d506#GX2`L5=V!p1WmC?1MzLEr-"
    "<jS6EEi;(=~Wpd>tZmLLMow&0(wOqVy7W9N{Lj8iKH@S$fPbQO<<9x19KwEmbz~GbY<w1{u)eiJ&4q5FUV?=Hz=EU+s2zd>|MzKF"
    "A#@A(N<C5(&`EagabC&ZX9<F2s~gkxL&4@Z8cFp{PU>X;cijQCMdac?>l?Er)cN<c>a-YLo{Huk!-"
    "juh0VORS61hIB7^CqF8{UeG~QanVh%>JP{qvxEG=|VdU+*i%CqG?SjeZ_tA=<70aH^zUcsQNIxp=wW11P(QVb6Su3u}?&sa+jqyG"
    "nBtQ?!0|G)9y+*q^!X%eaUeaXE7?w^-;A}dh(7mlC0N|vNE@Qb?Q~y?i&?laI%YI{tWx7q13xmoapd^wE*BgF<Sjt-"
    ">X=6`TQ!$(`{pV#HXet0g$YQFIT7YG&Z{*y&NEXCple#YFR?ou5b2x7EY+?Aj<s6Df*yY5}WlN;g($b1KrSh{4LDt%2y5DdqejK="
    "y{j2^RP%iqLB=?OEJ9t+rk_i)?yKqC*2L6XUxzUo`X@r@(lSfz!z_|Q5Z}8j;5V?3uUJMk0T0L+hpB0GN2ED)Uf#HhhW{Wyx?7JU"
    "bJ(=utDOgHrNZ#ss=x9NMPZ%Ry@ncQQn|!mBZ}`-"
    "ma;oI1OLb3ssG6~k2<+g!UDv_x7>X^+72^Z670oO>Ln1sUVnn}UDm<Y2wS>@_-W~C|5qa?-"
    "p}<nU=!qA1<O_R#JXlC=Gwfv+_A(2*AZebH0>`nL0jXZ<V9V#vtCm3zXLTER0H#}W!1OMd7Eh$-"
    "$dqeNsZ@Fnnb9rVN?UIQGV1BAl+3ATZK+3&2XgCWS}Mw}SFV-Rcp%B132T-"
    "#d)Aiv)Oax69*sF;!o6~>q{ahz_h_tHGw(S>>b;Xqz9;tNT>3q>Y)U5JL!+iG1)oFY;BznupTo28IR$C>WDAx=e9l~JD8dBTbCnJ"
    "N&J>w!R4yT(Xv~<B&oSiWbM_pOmCq@emd_!Q7%;w>PGdj_AIGujCkcCCE|~I!Q10=X>hT2f{=vr!%4Wd$CKr-OSFu!CjS}U&8)%T"
    "SvhXAo!cM)dlTFH`k?x(D{3K21#?C+35}Bm^<aqf+xi#DkFY=+uxw+0A<t+EpXUdvyY@(5-"
    "Oqd*R=4$953V2N6^g0gzMhdmp`B3>H=RbSN&;E@Y=0@(B&~nd=-"
    "kEo(F60yW;n$pA(|O$4*s<5j@U80#(v5<O+>8Og3g;6K?D@)KRe%)EDiBZO^O{rtLvV|eZ@1Mi`Gu9TQHxx-"
    "*ToMzz2r()r5J9|EkiCYO)7)*>jdj9J2;)h>?PrkB0`M)fJWf=)7pEGq)Nl-"
    "X>fWPoL)sZeGN`ugVRSib}%QjaOkW%Cy?mX1C!(D`3WX}c-3(Er&6H@-q@dAcEN4kE@R~Tg71s?FTXxbT_({?S_b>_wLm6{6-"
    "T$7r3sH15vD3b(0hf-5;JS!{1miFBX^RRQ-zm?!sMK5#)%;EjF?r$#*<*+PR>o{2atxU+u7<fhNB+|W~ChAZ?Rzm3c<x!!B7djMN"
    "5oZRHsT;d$yuHayiol%w}q<1+)4&k@d6J=@&O(CfZ~zh+p=Cud0Ht7O&0qb?141d@w<SADD%_Lw6V>I4;GtoFEi@g?)D+Fnuy{2A"
    "&h4t7Yz{=d#A)#Gjr#G@wlQRVV2{4y+n^*{Ttv<L3@M3Kb5!jO>FFFv|x?q?l0Z2u(ZVfKv>GR7*%i;mSSTFH_6TaP{NI<1yY7XT"
    "T@Ih^<?tk$sk6QY$bkEBop1l;SnU@!U&7n9a10r`;uKpG$q87TVWQwNaJqb0jEp??~4HJN;y7Pv+l|4hK;CL?*A)yeR$c(=s(Qrr"
    "l6vGgK>8_g`r_XV5kvLYq(ZOX1(T7-_^7n0eXGdLeXgLr_Dw<fc2v-DkTp_%sXSP!!1f+uX@*cNu|y^wMz$aGf#RjB)^ni-Fg1&S"
    "@FHdEzc_1orWODR<sH!fm@9%p_<wwIetx>KsohPf!)iVvOGjCL^cUn;$y07yV%vM8SUSyy`yh`OeFiYAq+Z2C8n`fWhPR9NTYan1"
    "m7KmcsJjrH?tWcGY!lMY7oMG3vndlYq5)S;_5mWx=peHs!S-"
    "Y`BqJLC{J`da0*Zo7KdZI3JLlt8t9h%~?tqeV$vyL3`hio!6X;GI-SCdA#vlia7jbrC-Bu4Lai88<)&D&tm|fIOK(3|4JVzAuAY;"
    "Nku@$YuQ+g*X#-EsdU8Vv>_02fS&lTHDzP)ZpT=>PZaO(SfHcJ{%cLy^CIZ^;{9S>0Z?^EW@X2k=sN$JA^W*0^LY-g@}3-"
    "d?gRoSVBvMBC;`#>9w?18wlxq9<?)YR*u3tJy-srTLPz&tJ>6B0$UhoMol{P~gXaP8PhqxeEjk2zttFJQ;blxY8@Hngs~6Ur9Tn8"
    "P5*U9rU&Is?E!a0VRT%ULlX?Y16_j&K{vEkKLof2;^X}9OUCpm1`>s*!j*FG}W%0Z!9kX>}?cE82>9s(xpcf$T7~LQ*hXquF`FAB"
    "61h#Yf!Ydf#2DuR2Tyr2_ctZ%1@RGYZSRdjba7NyY$$W9(uzwe!=bY*8&m$3D0&y~IE94F#m?aIpM2xH#8t+6}%v&yD0&*3N;qEz"
    "GkYmQ6^#8tGW<$@Pr0T6>JwpgV<WJ`lf8VE*#OLE4xcjgM9$u{NI=^o{USSxzgwE1x>FXWDC^!a%je-"
    "5NS?QyzB1#Xc(_Xf!=tT^{!-BkdQ)x2{_ty9-BxT^{Qh0)N3<i5?5(K-"
    "L#g;M(pBNSb(IGr&kM)o}7<{jlxnA~YO$_ag8y(CWfw31m_KMhfy&}FSsEET^TdQ{Fqq@1i%K_||%V8%9%F%BV+ATclIP7f;;=wf"
    "i%j9v%9`7WNcY2m{g2Vnc(iJA=U&@Cid$=PX?({A7`Hk5MJ1zdAuf4IH38~w)wXP;_%e9ZUaVxBg>$&^d8*^RAaH5)qhZeq)?W+7"
    "o>S=YI4>#Eqd)(FLx=0pK!jWLIS;w+$xUnuy<@j-aZ}1~biMn7rz#P({G9phlfPJ`RAi_HvMoxf9fn0`M*Jt{GKVcFtwH-"
    "{jqpL}*s3FicFIS^nBSiyTSy5wAZ(pC{O$9v5j-"
    ")|2PSme#7cT^bz(dX>0rn#WG;`rPC<7QYXK^){OhCV8eXOZwYX)IDX+|r10}URcmD1ppjZK4zI55NGpUc7mj9FmeX98CKjgC<sgN"
    "RV5s}GSDl!$34dCwNVDEDy$6o?c_MG-(4jxgu@uU?VfiA91SR+<&-"
    "P#F&M)6Gg15fl6YfFr&VNA|OEC@K8$#Z4Kt$F{b%YL!iTnI@?SQA?Y(6)au3qyekk)FboLVdg6^!ALg@>gtRfb53U@AcPt{ML{M`"
    "(US8Z+l(uNk}pr(<$>oacslrOjtn*)YuBK_@w=AW0G@#i<)}yajJA>8I>Up->}G^3Y{-"
    "4X6$?;%1F=b+=hZ~k^J~eg24$CNIlZU)_U`d(>re%u^qO7bkMp^U&oG7M^aba|mo8i+i%@x9W{}jw3vChe920F-"
    "woc!=?#*OEziZE5xp=X272Y)RH=MJ#oz#ttNfk_I)!=42o*js;z8{QJ^_~sJ7Z2zCRLvHTVD4?wcA6n7xG5-?y^w7fy3V|@*-"
    "B6(;zB+rqDZpupBJg~nJl*nk<;BK843eL#N+x^yEx5P#FxfhwupcteK(SOxeEtd@?j48d3ehM+c~4gXBH5bAUFDaUIq>oa?pTtc!"
    "L~G%P<&f1l&2{vVZ7Ju3-"
    "KwO%)Ugokrd*f9PpNAS!?%rn715UQ)i1Z6n9bRBzq+9PZ;dV5_d7fQ^Ewpx<2Bf)8vY_8`ABV^Wu7o(WUk%<v5tr|u@fQalWl4iV"
    "rK*ICRVb0fo9=pTMA2wBHr>P%YwZ77kV7JjF~Y1NeJ`X+4cG93A^>IepoSz$#2)+S5dgN=sh)-"
    ">tRCCI=R8M~xkM#tfbHv&|1XNB;HMsjDE@F+ZmDI`wO>(3ku(SSq~39zLZkXW!ZBz|k!wvNWmof5GOI~2)eF2L0Z#vY1=L2BCu6o"
    "^X?whaR-*|+>fxJA7~*%4E*!M@I^XC6d&8-yaUXfZ%u69$v0!;dyi^u!NUxp;|S(FZHp<&lfG<Tt*-"
    "H<}hIoBD^(dxyA}unjnKaWB)1ZZ1=g(;$csuEyTH(e8G7Td;4yf7`NOd80MGp@`9@7VAslz{jjH@I|bTmQ#XLDctdk3of|RInUoi"
    "NXV+}h?jcS#Y;ZGO{KeODm!;)(jwN&U9+l6MZ?hZW_WeeWp_3!?gHBeE(a4gJU1KjFgSG4Hx+gdQ-"
    "x)E?>1Qt*`YTWPcY%lWp5@j8c_l~-)kd=2Ja6RF6ir!Of96c+hok<Us$ke+Ky~ax|ZvU<=~#`w?WqtT}Y?*5gQct3tto;q(~$aA-"
    ")H2&{J;_qcxIY9tvhEvqn;DhYY!iP-$2ljO&!C?uerFLMP>hUQyZVB2Y`{ujYwL(Mq8jX2vIj@QGoQS}3Ix5FSI2d+2jia-MLmT6"
    "n2Ixl>+uI`DD!c<Qx;;h)n!G`ffT_y@L>En1UdbkNX&(Kls>_^p>}NyXE+lYDDW<`QyB7|B-"
    "6aNX#!y|v}^@xMJnXGuD#0MH15tBRiYCi9GH^PGBZ%s1C>IUC)U@XeSmVz8zsHzS!ll(cA2@Wf4WUUcvrT#1zF7kkq%1J70_(>cL"
    "_Rs)}|Y6(8($^se`kbdbEc$?`zUarrbA*e-cX(h*wUF+GN0PxZP&a;fr==9@~J0&nWV=n-EHIu-Jmzd``h|Iov8DMaV8#1jp1x&s"
    "y1eP02h#7CR21*`ga7?GC&wyvkX=H#HHbAlCH%K8isEW}bh034`4l~^q7z_Y)4v9O%F?DHwrDOOkX?{zupkk5oT;i`xzDrg8my{X"
    "<duIAD#WE4MaL;B62wbE`Tx6Cne^!j9nT+bzPEn;jG2Uh}?pxJ^{@A7|?{3K5njP85O}{SW6>&vY!9Z18#CYYmosuAk(HDTYS2FM"
    "invvbwO~?zDz3OduCuz2~LxEP8hfAL^CW!@;Ti$u~PBbFPmg<UXFM$MF7*$gCMlb0e8duLS$@nbpPS;aFQ!bQhOHQPWWwMt*y%Sn"
    "&p@+#>hTcp3!e!KAyB11R2NPFhP25y38txIy`vhi=UG{bjXB~wx8bsH-o!;h-"
    "IfSCpdR<?4p1X9_@gwK8Hj_$l!*Z@2Fg3pOMg*^4Wv@HVXFW(B&-73z{3`d0HUfmOCw?zSgi12Vfo-Nlj^O;c@%fhXYRh@8>9qIo"
    ";kB02ezn;#Pu{#o8Z;wx=qQ<enza#imR1*|dEeAbzg0FvHE*VGYlaENWIJdB;}20|&YGNCd&~AUID_#+W#O_=GZa3{jU?7RP|V+X"
    "ey0k~<1Ig8dLFkdf&&~sx8axfL;)7*wfkKs1omKr$aa6YH<YWJlO;ffy_~%tyWwc!MUfNiBQSoh#zlS$O~i|Vcf}p~i-"
    "_|Jb^9@!4_b3rN42G6bKnBeD1E-PoWN;m_?(cL8cDM)NB_+b+;X;T^A(N<f+-"
    "?19S<6r&0k)kY~$V9274tBFTd4RfhjpKm$rf_9P4S<5XjHgSbCoBfLl^cUWF--1YRLI$W6UCoAjx}eZ=(71^#mAc_PkjEO~U37T9"
    "oKZD{aC9$jY*jsu#um=}H2+#k)((Ho2wI-$6`><avD<D2Fw*<o~l-"
    "rQ`JfHT(Mj1kUwB{%~O&H&*Ia&XLGDrSa2`buAOfigzDr61Bx0yjS0=LeQaNgdme54>sPWUt7x0vXjKL<?sW+=lzj92fJ>vk&u$%"
    "lO*Bn*_J5`x>Jgf4Z0oKE;nzI*uUueHU`U1-"
    "BVbbAUm@4$(2Q<#~^ZJA0fjgTgZJC(nh%Ek^QOmS=H%DUM|$Tn$8KHq>e_F(C!%9@rocTQE}i&z%+wl0Em~xv!oziI2Zt0cH*cFP"
    "3`UqYmZdpBs{#Y-"
    "o>z`FY{nll4QuMZ6B{&B1k;{2ai~BFJ#}p&7Vj6&$Es@TsR#t?W;NAe4=W5GxSapEkHva3N<q_f=XCSEH}etG$4@<ZN`pmijqPeh"
    "ykXR#_qaX`Ff*6p&nkHwH#~80!y3K#IG2BF&Toa*TzbPwX(5&e=@8f^7M-"
    "XuyveMN}vaY;M{b4(1}ZQ2pVH!pc;X#75r?gu_e03@|x0n!6x&l`0=_rE436fIeI=n$gAzs>5vt+tL6ZEWmul-"
    "V~+M0at?0g?`Mc=CnViNcPIqhxE66gewNy=WL*0h)z#cL!*P1Hm6OCIA9hYrvx!vJ_u&xGQ?1c5TXONiO-nQvF)6;Wd}+TOKv94S"
    "|>sgP6di7oOp|8y~Ru~l$acH8)KX|hIgYKTvXNkHG=t)$t`#%9n(^qo#_JZ!J8~0<kV%`LT9rO*cTQsp)+x&^lTVR;igNe`sOv`E"
    ";`9|o0!K;d5rM0LOA?u7Ph#Eyif!N+?6KDZ6^W=VH>0b0TTp?Z4f5JbIt|EVB5wMAkkK{Y%ZWO$H8Q#Q@j}Sin|=TQJk4o^_EPH&"
    "1+uf$>#|hM89G5w@VyPR=*KI+2^UNJqxaz6)C}~{*+4%&b3Z${2k0lXG#RFC;bK!PaiLaC;|z#xpu>l5j>sz8)2?lV5+H<Yecx1r"
    "dn({W*0h<3>_0@W*}nSi1p^B0l2+IrUNR=K{>zg)YtCkTlv~kWf<cN6o5kB-"
    "~jukmabuhBQ1v|Mh+#b7k$e3yPgR_yz^HXM+&`>ZYD^wZpb3Q9{3$cT_d<qc(}?j@C+JMe;5VFNh8UfJ)$d=**kZ!uA*Hg<#yENx"
    "_bPS;riu!$5`jZ7b0*$*GdZIvuSI=#g+qY%%%=ZAni@}OmKLbK6oHb6RR94@V3M~qt7gHH8!jk>UPd>e2TD6n>MoY?MYL0nfCh4("
    "wkM1K@fr#cyGY=h_8sAG|f@Rn6^Iwf$lpM0I<b*!DmwzW$bzY7&6!3zf1A&ot9%<tMY;(*alKS(f^mNNO~%;K~}LL<S6#|a_oaMr"
    "8mA7TyaOzL3%&9B|rhQ?{#`xL`O0%uUx$1h&H;v`rd6lb8jAZJ@n|=g0bsT(vH!^JT2Xc=25CiHI|k2BnF!n&@&G&71q3Sd#wcZ>"
    "!cHrR((HRn+Hm00N5^StIpkF5)XXt!``sgKtCrba@wYh11}cZ=$r=y+)N9At?2qd-"
    "I}{|e3luRW6_SwCM}BE$IL*jJ^2sMT6J0Kn11^F2DrQkB~BqxhB90sp=zPOVZzF&TliF2JDby^2A+2(`_Bg8d{dr*o22U?^zVR&s"
    "Rk5Tl?(fW+9)zD!)~AlEA*rg-*GXT%s4$0f9k^#e5SDkj5^oKwAjk`v}u-"
    "L#hI!GdsFPD@}Sv{6>VP?z(jCH#Cnz)1K%0DC$KXX?qs9?OuN`52yUK>ZRo1Hi4hkKs|`R?V+^|)Q!nfyivodp>652Eduk_-"
    "y&EQGqqm^*o2=howr;9kF&EK@J4<oNg_mUmnJ^;a!ir3{L}wBH%ku;<sP-"
    "J>j&Yu_JZE0+AZqAS(ZsXKr<^CAaP;}&I8MJ63E8^_CjYcUTKU-"
    "j+P(B)Q`QD(nwKDTS#G<RKIKni8g*}d;+sZx=<1P1?l52o`TyJd`X5J*<IsO)#6WlCE<cuBoqQ&5S2*2TvXYbTaC_D}o^V(U$yqK"
    "XlEci9yPQ2O5F~Ldzu2(xo)tTJ$#e1^@_~HV$_t>p_$z?(r^I_F{gi(pUDY4cJ=5LO<Wg1=Sb%OhGu_qI)z#J2)zwv@aXcX@hQYN"
    "5qB+Lg!l`0YRd~?E-"
    "!!|Lb{#coUs%Q3u$jy@XCezKmMBDPAk(BNNh*%M*4TvOtOy3TfnkUe2jSDtz_v43L_V8`yD#t9LRDunsY{n>VE&IYH?cHT^!9qOP"
    "oR#10Z7|Bc=f7m^=+I*;V=RZsy#JBblN$umi8J<O}wjC7bA>hvzN``&Kt8;W%w&1KQjZBo5i=uz~PlXWUVTjbXyb!qitWi;Ae#-"
    "UYE(Dvyuj&?hUaOCRZzD5=65y%FHT5wt)XMOLXy4;j&&UUL~9#Zh5vDsbl3m!)?MGN7@oGtlFd@r>Sm+=O;$tRa<1H+rVZT&BZ?-"
    "O%`xbOau}#sxZ>ZpSx<jf+}EKJ18dLPw1lyo^f{~IBam6*5SP{Zw&q8AdMa?Nr{_Ztbg%FRa!RLi<BBhMS+)aKv-"
    "$McrPWQ8Ir{|+l<T3)pb`d_9QClVM~%CQ!S{QL(o<MdSq)XU)6o5qZ52r+VVIU!d4YzE<()ZR8%v^nMgUD6SxlofABekC9nwy_gD"
    "mmb$$|pAIWvR7Jl8pV~n-"
    "hUS2|E=O)>hKMW6oVOf|fT4jdgQeb;d)Z(g1%!}x81)yP)7}atfzM+vaq_Ig}q%D3K3er)+L}#i6VOh-?6_Wg`^CzoE?NIp-"
    "7MqdEfHAx`a^Y2EKSHh-tpKZtYl{Gr*NE&QLJ^Txj&<hATZ>7L%y<e*Fa^c~GJ&JRMm0=Qi9JFT$92cw!59USk^V$`1^d}df3}`%"
    "exb|CC_utC1z>BwZCvKIQbe|#W)$q{w=FB5Bb1haqFD}gNUgLg{>NUmv@+whFzN?Nq;H3KKWcCf_r~Dr8N^aUL8WO9j_ENn$x{m<"
    "Xowlu9KOKHcM40V0ck9C@CeI>!YW%K*Dwx6%7rn^ogAI@dc_turNAjF6aJG6F=OD&IGOg-E1!ga#Fk3i!+cDDB6kqSaQXQj(-"
    "8Qs9cDrMXMPk1=>A<+yRJR*St$g+)Q}p4|FfnMM`Udhu@I`*i<Jn3b4c8yK4q7vjrMPKDAH^VO0I^g$Kb3yAi2~EQhT54Fu2Z5mE"
    "f){<&GwL7;bJ#@iaS|nNt)w!=%Ed1POLN<5{Jm@}Ff|@!`<UtCpGpg~427fJ~DNTmO8A<*L)HfXQ-"
    "*=F_GSSlRyOoFB9KOK4;T#qTgvC!d0Z)XDqzWSy`v`^bX;PSag=J;gTwV==4}UUnmg%9{-eyQaR(V#VfJEJAV;7Sm8Sf6xLET~T-"
    "X(qM$gP+8{Cg@C;?Cfe&N6<!C=2ZH6pa0tg5E>-"
    "RZL%fcL6?6sQl*I74a6;$3Xl)+1+0BJEgp^}oMd9JZw03H@A23EP(&N*>wD&e>siPnc3Jv=Aimi6CXHotn|6UrNRBh8_sDCfHJz+"
    "^?3d4+AkBsSI{^=+Rry#8Pea{smopN-"
    "v{(zad+_>+~HfgSS91dxWeCK*X3eg{Bgi~|@g4q`|_AZ?XR2j8ph7Hb(t!u^2faRpfz?LzR7O+x9uQJd{g{P`qaA^*^ERGD1jIPC"
    "CSgGUHxZ^OI2sT_jFf=sN)ixu1K{nRTV^cfLjdW)a`Dgbc5cx>h*WsYkba(kF%_G|Ou0Oh7rGW{^?_UdU?0#|g26?k=(*XbuO!)7"
    "lN^b}9=X39(S_Hl;gMVq)-+Ac^Dfq4gzMBExbprob2Hy;B-V7w{A4}jLXTU!O@OkJ)-"
    ">%HsQE|LF=b&&%N#nFBKuoC<zWngbyEnW$x8HjE_90|Yc<uJxyRY89^BbQ}Dg~B#LITr|0y6s<Bo5`eTUdNr*f|ouzBr;a$3!0Z9"
    "X*;!bC4$WdTVgqe&dHIa5RUWNhL;*XKZt&)kZtUYV-"
    "o0rk++~&he3q+y>NpE5LNwqXl*kepzZV1c9OnFRlT9r`rU2%%TaN9nN9J*oA-"
    ">;T)to<ida*kv7yC`Yrj>M>M}4`PJ3raiP53KM4j8DD=1dx2oxzE`4LHbU5Ph>GPcs7qw<J{oc)d?`pEaW8$RvAo7PpNzJW-"
    "$RECn$%zf9kNtT5CR~|gScqp+UjZTpfWJRFMSn((XG}WZ4a&D+R>jc6#h7tgQK<5K3pQJa4xN>Pl^4g0wsu)>{kKjVt??iq6P-"
    "#7jah?=G9q0US=)ve)Mrru#SemGe=&xu$hsSjM%Z}E62cQX+@_Y<(m-"
    "*AO)crP6>b$bOKk<aQo*jFWrdx2>&#*J74I1K)<}o6Tp9g;^)6u=(iYw%H3~$e^I}$bl@-"
    "$n_%fwavUdu`#KfU4Go8;xsqKnk8ef$PE8N{5Nyu35mSNIY`F4HEV5S<0%usTTuGU``?-)tkO_-"
    "`>6d+Pm!#jtEwHbt%_n_aPA!*_o&RF(}kvYmG5dtyNaqjtZkML!b;DXmUxc^2yjXXvH_rowEVvDSuW`KSacq7CSQ%jD0$Sg&1gRB"
    "}5HS+`_g77$G3xerrIIY{lo>H5k(7_xdC5R!x%uJX~J_U2eERM2KTZ)#@v3e>qHG+dwEiY($;mLt^_*OH9vR1JERHQIz)^B=S3f_"
    "%uvj#}0j%@D%<8GxO&4mev?g7u*3)@o*BK9BxUJc0R69|%#46g`sSfa)#Rbbzhd6VScoLht}uAZP>aa!!uUp5z`Buk2m?G?KaK{@"
    "Nq1DRBC-"
    "9+hBVZc1BefyYRv@9m;+NA9`pkd>Gfu^1FWuBEOQ(r<ts}u+ZvP>JbLqn!kpd~7_#0lDkV}SR~D;?`Dkal(KiotEb9Zg<1pV}gzr"
    "^QwZqM`bcy7|*zWr@I841DNJVRW{#Yeu?G*g$q=kZKg2t;{xFiK=6|=1z7TOvkF?gJk+o1O3AXo}qB(Gl&@?B3X0Mv-"
    "0KkqS)0E;MMM`+cW@B;~=~yh=C?bz=Xa@=$phIqgsisGgxj7AY0!c*%~DFaMe;h`Kp_dyVLv2bOg&~%A7Sox<Z?!ue$ImA+Hjbs5"
    ">~Nl)RPs7{WBhj*W`()StmT0c4>6%Pp)^#SdNjA)$o957zME>akV)o6%f!7Tc+8sgF|6u{r*oKiXDY(b_F#;=n#PGm&`74xesuN1"
    "0NzU(N{=SoEp~x$EdRtJ!ehJM}NzAbq^Cs<?svA+AJ~P&#vKFKyazfyy-)6Tp>3OT~~{+~6_0au6l=6-"
    "^h$sW5=oD(mLiNf3<P)=bh=FrNEenCTU2Gu#4AzG+It&Wr5Szo;p~jE?GM{dB&<&3h<PsjRC$tZRxe(rO+q`X{;kcs7DzjX=q6W4"
    ";LNVkXL&igql86w`&?h-0XoQTSLlAXJ)J_tWTqxq4j(&)!uBWG$-*sCv2Kd4Ud(i@hXIkrhlQd9q}!C>!5^Zg}(Qu<>-"
    "*bY6MGx&CI;xY69?R&tZu$jxmbH?w`bDBLAF35|6G#^)&fosxNO(z3knK3O`ZJ&qtiv#_n9uSsk>Q%X<rh&;qOO6HY(oJU`Gi(<k"
    "00~k~k6<Z<)a09Y&nrl4rSd+yyl+!?QQwdtFbkw6maT$t!w19e)0rf}*^(X`Ckr@;_2!)<>)RRykhjS!4E|hHP=ZynwA~GysOVq&"
    "y{c7I1cmXssRXta#B6YW=ssPJW6;{4PE{P@xrP)WuW?M>S9ExqZGJrFtJHxC(d-"
    "`lcCI>$zugl6l1(va0$T*K`Da7ZX5LUe;yliLpKM-QL9Kn_o#5`<}5Cem2PtwjAS0L_jIZ7yiZV>?d*0vToKcId<g!T^iY>f<TI5"
    "~W33$OPmdxU{u%&Zalx@i?g5?dX~&SWW}F+QztZKB`oSz7(+U<A&95d7S1{3Xo8!PS>dGT`E&#R9%r0@oAHDT~_Ya_w_o+aY(F8U"
    "A_Os|omt3_s!U4Tfl&QHGwXr7D`B6!rb&4~hJ6UT7?%U{Qr)d}phXq%{&zqw8k>aW$uzjW}kCo2)zb8g#l)Qe81)CKGi4wJ<6^dU"
    "K`=RqA`iMz*6X4^d=hVKmo!Y6($GGvBF_waW57W##2XC&zHKt<<a9)h|qTELYS6jSgcW---_3MLy@1;#eH$aNCEIYbu}boGhmAX_"
    "LW8+DI@CeJ0O^xRdfWS)$~^<y*8ZKfS1WG`wptc$4Z>HFeCySAVHl3R))T?i6NMf_cI+$xA?Wi-is<ia|=2yhQKAd#gN$w0~HtP9"
    "weA?C;^39PUTXgyofc@b0nwfWRi~PDmH!t!ECpzq|r;DidKK_rwtmdq;+Eua<pDMn_YyyJySV4FA1@WO!6Xd>=i@4~RWM>pO(RRi"
    "K2XW>T2i^_?0v+pX3Ri>?O9c_9#C5P+D*d#PrhRINwRsm>+{Ef^(zSEU&YmSnpkQ9lj3F5aaDS7@@UX|l^8C32HpO_NeVdz02IN5"
    "+pAwqudB-"
    "}`%t&><Vzlz6xH6p?f7nkBrn=#>Jns{rgO0J~Ps;F9qjDk?^UCwmsluh^rr!uvawJcL?vB^cYyTe76%0m*Y<x9wn8IWU@%9hg{e6"
    "frv*Lon@}l;z9h3oqp9)iJlTn9|EWu;wsFQ{yTJ|K-=idOw`@eF#C$F|Mq-7NFr7F-dWTE@fU(+A3($9HkVx`-z-"
    "(QBUzVChmzB<GG123RcGcr$FcrPCVL29+<w;u_}_Zs`Caj>py%|3yJ1F7FcYU4O&y7(eUo$9mmsv8m=Y*1a5-"
    "3HDCe6{G~ok(M;914jGIgx(sT8X<#g7G*RhvPV+buZLscs#c^eobh9@c`|~AC>+vC+Rc`ibBM1E;bwgA&Nkz-67!5=|!EU`Y4sy-"
    "+-<JW&Cw@EDT;AwSgV~=IHy4@+8Z=ppW+XSbqKgZ}rO#xxwk)Klcs53cBp|rJ)vl-Z#pRUjVQHr-"
    "Y3EKmMV0n>{_C{+r>kWwX3??VX5k4mxwbntr!E#Zf<vejc2u;b=`a7*aRj01eE|s9H36F8Xzit31~OY<yVFUVZ5oq*^+h}*D(1s6"
    "S!=|*a&a1S(OwD(6iZ!6j*VQ>Uuq{25u68+f5yb@Xy6gihoTZf*Am@7h(zjWKgKUS!L%XuGsu>B9)<4(TyW7ey8zrEipFGS@IWHX"
    "&g!;kw2$31**Vi)8%rmjzA%T-"
    "4u&yWSBm!S#t_`WEWX2*f!2<t6xhmHA6j4;Pitxu=drBvBg%Fo@Z^zdcq2>Bc&G;?tYLH8r1BQl?Kpb#4Xd8A14cN`E_>P9w#vX0"
    "y<j++-"
    "uEFaK?h%ev%<r$OkMQQ!L1{h2i~{veM`KjJXp484xQ~vC}M{)XxcmYl6?HVLS<_Qcd;Y8xemUB?_bh&(8(1UW;z^A)Z#Fji)=>*j"
    "|tMLLplevYAwu<RoBe~m<gRhi!J3Aq9-"
    "i|rgbGS8b{Tnxt<?t(sFP!?d983XH!wby?nzt09FkA(;>tNfEh&3sNl@2#eRe6X_?#}7&Fja)l-&ImZy@flCHJM|Ku-"
    "u?vZC`sEz+tfl=CCtY2F%OuGUf(|2BviIz4DqtX0i()J+33}o{q%1GiJk0S6o#H2OiB{)RX%^Bsu68@$ikx3`p^ja_Fye+jvlqQ5"
    "AM|RIm^q+}!227-ra4-o$g*pl}`m4o%^~Aeg%WQFC$pRHepiO|3j{apV69F<2AQJ&HDZsL;!*WfK-1HB9-zN)NW_Yh%qm;g-"
    "n!vYn#jI8}#MubFD?z8bqzrgheiz&XQaR-%pG+soeB;+~_SU=}{;*hXPL&-"
    "PS3yK8baQ3W1Tp>rI?j3lk?ca%^QUAJO?GN1ZoT6uoZx&UyK0z%b?@Pc?iOJKoX<%uaFe3jQVtL<ARf=hi5K?!<HaDLfMc&0oQy~"
    "*P#EhLQsow&!kGv27TJo(Bg>i`lb&0z3+8f`>#qLGdj}Dh_5rVnL99eh=z>jBTtAwRCIP4bZ;&nI5*Shmb$b%{1N>5GMc1IdQ*Yk"
    "rm<`7(Z6ASr!*SU2$G69`6Cd|DYJ)bi4tq>D#|_J{SI%s(t6uT`NZQ0c$ivHTXIFta5Ak+GDqcZ<<^tBXcCyM{xpE>^PKqk`!udR"
    "$q>cBa#(NHp)B0Vxej?Q`Y=6iCy&Wtu^CCs2h6Jgm9~1GhOFwokb0P(~B|+boplwNj_kmAaHwvc;ui`Sbm~^81I`4%1KP94!-"
    "9|nO$wq*s$|bvK4!2$i?kzux!ZWg`ibk`T?7b&FGHvB>=(rvCXvD}uXlF$4!Tq*4BoEodJ@yAd0XEWI<X~`@8PuffG{r|)Wx?Jx!"
    "St0a?OShq1#pR|m`LE{w?(I2OraP}785Y7#!9>b?-"
    "j4*waF$~vkdN&o%jw!Wq8SJJ7BB_(WD?=HaBtu3*00HnyrdcZ`i8Hd!vn|D)*SZ?gyiBIXlW=oGxxv7!r_KsWE~6yrp2n-"
    "dd~*IbpYhnF;_@LjhIw=5zyy-"
    "?rhpfLfsA*Z{08PrYSZFyOYHSLZtrpG)x7ww?tEz89`7_gq_UI<?$%Zn>xQxtF%w6D{|$eeR_#_e9IRRr}oQxwhPNYT3QdlMy(*l"
    ";=C!^JZmB?~|xEsuBL83V;L4qGr^Vf)o=~q#kXTsS;6-"
    "YWQ5)BG^=_Zs3Mla6<uZh~S1Rf$L?#^#r&cg6mOm#n3KRp&$?N3RYyI+R=DxcSoS_^H1gS+$a1n?~(Cuz<pfun80H~AA9h%C%*RJ"
    "Yma_)a-xvYfNKS)%pDTt0*<V%ldk*a>)U*|$)H`YN{>VJu}2;W58bF-zhcwPHJjeK>DF}i)K5XhvA)T)2*Xu{K~gP?Fo0DYHb?lZ"
    "9t>?22UGzE!(Q#Bt@OuX9LU%}_sWGnEf$X!Ys3+SlSh-"
    ";IA8{U8z)nB7WCFCS91A}ZV{$RBSt=r1fN8LMWU4$)6!1UwH+5O%yFRHu0vfD_hn5}ITL=yVl`_Ob7Z!5uO(Tzbl@d3Sn~=TYTg?"
    "cZ+l!eD5!3Lp}<89r-b!V!&<sMt$I%6RGQPWarrvj9RdC#e+gf)r=jxHgC{$aSz2mJE%7B-"
    "fai;7_Ju=}nnOt$3DY5Qp`gjbU6oL)9nv=%s<nC)dp?-_7G!%!BYvT&v+muGg40pBh{p*TeTeR6gvT-"
    "`j)H;LOUTA*#_sTd%@5d&K=8+BegZq%@z|e1(%gASc3HD!-E0byk8}%?HON)&_%oy-C_C3356{e{a)QT`B##{tMI04Rpn~%_UL*?"
    "Qc^C!eV2y?(j?su7TVx}L6-2k@#hb;&yE*8!W>K_hsk|u{+4@nV(-_fSjS+waTxJh&nIs!Iy39gH)8j2sj~{Vu@fVcC-Qr89(WDC"
    "y>1Gk>ZVr*hO5EuJL>^}mdF+gcL(FQMN1@2UU<+<60_k?rK)M|{kgkZE+cCw>%^avnzF+k0N^u5zB)fuD`B_x$>Q~eL3k5>To=oD"
    "60TvDkP28Q_Nh3U~MU0@k8PHwoz#-f<vd4pXV?Zggf{U~;i0M)jVJB+X-NO1`=3%rm0J;=F*LY2spL@6oY{u-"
    "hG7_CA1%P9Er7(Y=vw~rNSQ~WW>cMttlqh=Fc!K`)8L0A=PdBhi=oz?ilSS|5%`_e*8v_SJM;@P>FUxSuiFU3M3#TaZxKYw6d5~<"
    "fj`@<U;~^WX{;-"
    "yi0mc~Q5_|2KS>%^2T;ny}Zhs|Ee<e_VB~brs3DhC%2jH5r=F;#$+>I~^R=kWZiea8=yltMof_&A3bLCa*iGj<;`BPHm`Z@eP-"
    "x{171$LpFCa@ZNtep;^DnFe|PbP0+KblM7QL5sNAv%gu@jjguFgdE?O_#pu8uM%Ma4Zg~?g?coFdv#XcX;UR`)}w%3#mYD(W_X|i"
    "os9twGb{Z`+bORK5I~GExP)}9?f<IyV#wI7eLBIh)WK33xeLyKhlCDJM3TgPyNGwG@8xP{Bb;ngM0$`L_Wp$mqA?jz7hwX4}>I(x"
    "fa|sjOHh`#SC^>eE;j6odoD!4{^}Z=|J%gn9Rbd-qS$MtA4kDRV&u<gh9{2N<)z9Wy>qxj@S02Ok#s2cz`D^BTnP2WBfyqpB#&t#"
    "J}6#jhDzsz3A<}O#aj?k&duVFt`ot$N|Kwn1*NN3SqxR#50r;7=}5D@<vU~f!A{&ph*Q}W<MBbGIwaI=nXYOIi&ts0f;r4&#7$JY"
    "oc&ScFwW92!L*rc8*gDdX2$zSXZr1P=1U-"
    "1K{r)(|e2gokefdFZ0GK_CV&TsM+?)e8Q&Vg{vvHV2iH4r;IOS0DC>Qbf*Gq%PWJoIM%7yqO$EtGR>uuL-rKTG-"
    "7~mc}>eHtB$w!4C!EUEMjWk96Ht9q#-(w#<rv3)WYZ&K3wp;J)4aa&6=%Az-"
    "O=kVOG2_n)u_<IpJ$skJqnUA>v`$@|wA(vfX9WJ%p@K4+4MSl=8|J#A*@_g0{y~mQ{HM$F_zZ7%NWdQ=2aQhQL!qD{JGwm~X1>y+"
    "ap!m>*9ZLQ!>WJ%<q=sa?lBS*wjNR;h1blqn$lSUV`PZ!IQ0NVJL2Xl%0g-"
    "zT%&q~p<ogrMM>oeC@ZjwqlVNf@!0%_rI$fh#6G6N5o?o_>%`|EvNtRRwop$YzS;shPYG3fUCAALXFsJC<>Y-"
    "pCFGoFX`06irTJQo&<z8m`fZ2Up74EjuC8Fq={mZ=G=?nfB95on)|tbY3g<opY9Qq6QDsEa__}kC*HltEP{)qsH^6XOf>oLn8Ni^"
    "RuUrIgPs`f$)X}>%&UqVL_DHE$LHsMcSNkP8kTTL+CD%UkuL5!y!0DGajGgAh^y-"
    "mE5lE<c=m9Up6<LxZP|s^NAv7m{8c1Ai>UOI-"
    "^uX{<BOeoe@*asTR+BLA@;*%r%C`G{H<h5WloRx#|=vV6YsbXhUB~Uy)9c<r<lz<xC}dAamDG<YqLdG@6{hxLk{fH40|B6v21K{$"
    "%D}oO>8O42NXj<WS*mFeGJIab1Di%Ta9j+-"
    "RU#N`!Af;@6Hx^EoJG<8V0YLlB1tKuiyIeI5pyjpDf%&Vp>3J<V~x)SjfAAy#tSAGsX;$W>5(8mNwSFnLF~Nq9J#1b1j2Y^6l{iw"
    "68-J22A>C_xF*-"
    "CB4WL_}jPDXl^39fXi$dit6r1_{L#xzbK4Lu5h}Ks8*<3$nT@5Ytp6B}g@r1$t5Sc6W9(^c5y^GPbYZf8^Z<2^pbv@-"
    "8I>dS_0!g^?Cc15ZM6Qm;ImEguF;HI6RLEQs)E>iC0uZ&LWm06?+jWPA|zxrC1DR8U9Z<ZvDlVsr3SuLN0R8>DzC3t}F<H^W0v3Q"
    "HQd_|@d0KYR-eBPA?VGLQfAhzKMamZ0h-"
    "^22%hkZC#c&yFyXUhZr9OA}dxVa%`0YGZ343Fqie2xZ?HJ<+B~o~vuy47v99oY1bfAZu5Nlqgkn8OFzG(6m+!r~0a7^YbN$0W+H$"
    "NKFcqDzItQ)`i^;LU=`4mElz##u#&*Lty4MSdKL?;NGeo0VrX@0KNfPDUes#54mO1a49>DG6GDDJC9sI=-"
    "h^XszBH_{Da1oHvCh?Hrll13xlXrMTyjaE@)MDUz_2|qri_)6?u903vjDUp<)`L8#7Dsc2r_9<~o{(M_}nFD*?*tJ7A?{tr)S8oD"
    ")nlgUgZ=pGeerGz}nXa+wBz&`yZKU1WF&$pWm8Nz}-XN<n4<S``@{tCC-"
    "|Dp(PVtNL=pHb9}#64m5a#><K#djUl^7x0b0z#=U}5i4+tMmWu3hOcsZ5b{tf9YkIyJ%_`iAHPz_tUBqIV_u7dQ+ZG?<iaWEL&1_"
    "V!F2Sh=k&)}eTl=*MxV|A&H=oMT%Cl~L1yE1D8q4Ftb&3rDpW|!@{SjY@;P_9cCLI5)I>De;UH^yv+mvT$Nj|^!qyW_X2EhHw+_<"
    "y=P+vicnqYcv2Wvyd{|75pfFBeLQHvBlo4;PJmO&NQs=uo{xvvPR~RCfnvJ8zB60FZtP7_>85$|HY1O?ikA^3;Srqg~a0}ae-CxA"
    "<$e*T`a28t;^xE>_%m*FP`;8!)q6a)=0>}2C?>8nDm2BDXIk_8;h7*t`azVf<R}N7K21n;v>3PNZc6JmmCK#VVC+N<NUq!R;(q`Y"
    ")H~X&PIxi2te>8em9QpTRY4F~C0`n^!%O|F7Ift^Z&~OWASb?nKWIv)cpJL~E+rbjUW<uH!0kA;_r=okOLxxgvn~bIIfIxf%8Jn{"
    "6yH8*i=Y3a{U4R&S%Hg!3eGjgFQ@9P*cw3z*8Udj&|1J>>;e>4815T)L5qpfqYY&6TEQ|=RAI^ec;9c_$$7JWmV(`Z+G9a9@TZDe"
    "oLF%LLoLp$TsVg=(B6<cI5d`m$f*EGYnfJlP1H!??Y<^Q-@@P<a&!-"
    "V(m;yt3SSC{)5+UVtT2l9(W0>xv40dNHBckh;@hgnpwQHU%tyCPL)RPJ!rNq@0gj}m>dL1!$w-"
    "MZS*SAZH7{(@p43^ChjZiU%50=~B*5)k`V$TW|``OXfJ{Mtaf%ntO(mP%|Uq41>-"
    "Q!s7qwm}+UK!ywJl=sV2(nP$5jzVFAmo0X2*iV`W*wDyLO!CW!B3;On;$X0!0j(ti{*6=J3{MsOx5`q>*`bDSOE;?M`w7mr_4ds7"
    "*62mk=qq0GL}}|hy8}(M2?fGEEX|Z2}rzKf4KuQ?7d0jM050Jz4^v4r*pb2x-"
    "N)aOUc2;qGIii)Fpt4T@tuc+0kGPa@31Y3$uJ9{Eq2fFb`mfVeRlr4gCd)!|1O-"
    "o9$_d@$aw#{RI{1FMS2<Q=9&&(myT!s7nMZPW`3XHF_fX1GLmnL>lN?g<038f2#CPi$7{|ci<+N4IAAP(=Nd;i?R2@w8QM5A5El~"
    "dvJ9byhqA3Svggnv$i5q$3F$;V!Cw{3>N)Bj*ix=JmsO$wtJlma0Mnj!iwkID22BuxB4qMQ1u_!R*T|Niz0fOYQZITaETt=NImJh"
    "0aK-Fbnw#~$RMN<wHcrZI2Yj)NxKx50z*ehBQ`Qsw7PIVF@jC(U=tHqoeq<^6rtB%cp=cXmQ6_5*<W_dMG{&hu@qUhilMY}P>fUJ"
    "#E-"
    "uuHZp?A<8UIe_^A~AkH!!O*{RMs2J0cUl}S2;3Th^Snx#u4{RNOqr3~Qom)@0$cf2irRkM3yxG>RXlRx*7Ozt+D<e6_$ROdEMip="
    "-LbOg;lH44`fENCUWI4w;|W;t1Uv6QvVXB=uy{m~fQevc*?O3sQ78y-"
    "78GC!CRlS>)*CfspIEPs;u4$j418^PTp@QW#9e%IWA!10LMe&HiO1B7=O(<3a`@LGG0^7{Qp&<-"
    "tNInxlh<tk;d`lTQHG_$5<(`nA}I9Qg7CcV>{5xwfd`UwgYkb`<0mh;9z#3q~qTCx+DUe$&0xHcDLFz^rfK?58YiQ`|IRK)5Yi<P"
    "a>ER2J;m;wIjn7JL6Ie{i5hf~kE)|*dtfV1PK>l)ow)q_bd7z~2JLw+9&&HU77ydVgT5xujZSDT07xaUXfU}uSr11GAx2XMMe_=X"
    "M6+VL&qnX}4yrXnxL0nuMd&%XPo&;H@VPyX`%KKtL_{^W;$j;EQ={@`oB`0>a8_LFaX^22|8{%1e<#XtYa<ySwx{Ki)=zx_AQ{`l"
    "+9KK$VMAN}qpKmDsu{`sGt|MovU`|zV@-"
    "~7S9{?>1oDk~&)@cRC|e^L&j$eL&9DxK_y(>Nr>NgH8=Eg1?jN>Ca|1bFt{k1xOWqffu{hyV7IKPo|7lPIcBf;jf|j^P(}Ur{C-"
    ";Lq(CPG$3Qxa!>yrg!;-"
    "`EX7a34cub>ixH%=RKqh%9ajpUt+Ujjy?#a7#`e@N3$98{L~6Kj3qap+9Wtm2?DFuC&1{m&FDHbK@)@;olooiu^-"
    "3a*upR=m*{7u78AT2Jr;VWOiKz12pdDTYr=8is{Im9xEtyh{gm;maP8G=;h5%4yi%8r5z6MqiDGp?r<~(J1xpCHO}cb=YG=I|zxN"
    "iS@!)N=m68hOtcpMiX&2?*IP9?{`Bn0>{0=EdcHvLGd4dWf8H8cbATPJU?;-"
    "C4g$dOnI12yDgS#w*)4d+t!z9mTfL<}2XNm!!mDm^%3g$$hUq6Y0V^W1Ql=}p!w3wn$LZ6_c-"
    "9eq;TgsIrQ?LJCPTK&qqu?}rPePltqlgskQlg0d=imAMvk$)Q9lrg#_x$_+=hLr!>+*m7OcDJ*{hya#|K{aC{y$Rc^1pxo`H#PM`"
    "N7BD?tw>FfUMxZDB_9x1>%Taz>ul@n~NYysCG;QdFj_^i5l49{n0ewRdTu|eVyYS_>}j5t7v@U%4U{f7kTF)C>}s~#%{Ceu=IJb4"
    "YcdPHhO*li@rY|_b`flD29NHF<gqtcZPm%4Q#TA4btu)*SU0PS_7j6Sf+Ao;6~^5Xk9?>)o5;;u4c84&&NOh>t|p8+2scx0p*{6="
    "f{`-|95n1)rDoUfwI6fZ12sG=nQ%#0o)3CIa<s#Ba0Pz*!tJRuDO)@)}Fxr;oc11#&sxllpYhm|32*YJ1e(?+jnal)IbgJ(c-"
    "X16R@qfH-YCR(8P<Y!Jh!Bi}n>c$%$bQH#J&D6l6_0hXN#E5PW57+&12OYco>E)&M_^SciM;fZ37QIy}=c`K4v{tzVW}HE&%FHiB"
    "{3_s551JtV@gPLO$H9!$!m-N6x+PN%rfJmOfEs@|_3-"
    "g^sT;gQGDaZ;AMoKLRKcV`5fi(;plgJkMYMt!owfi)SuQ$m`YBnlwXoa|OiC^UkEq^SA_gY;6C>YhxqmZo0`+Xr76861$sf{sRjY"
    "uBXoQ^Jo<`<5T-"
    ">JIDH&0;B~sdES`v`WG*_av9AB)D8Tx#dcUEiVf0AxL=~Ur_TnnljnG&C>Z$hx~&4j28~xqHCyv>94$OF6Vbe!k&tE;h70Y>EdU?"
    "!5cR3ZPcW<z8P<mrn?MxDkeHX>tgl!>Xz`Un7eYS{NYJ>=Fu_sSYsPw3>_8o2n2J|en#^wY<xId*1#f0Z%LIjx1>t+mK0gcvWF%W"
    "D@u5f8-"
    "=sSU_jd|?0zp?<_3rHY91xHG!67BIb7YEMR<I35FGo9@x1spnWfQi_E|KT87W4x&$ib}OQ}(cv{^2pl2R(Zb!SHu6Q{|T>g7rvhb"
    "VwZ#hXE6BLlV;o?Nz&c}%1kk+ReHn8+49JhUwzx|2g4jCXdPR90O1vj>jKy2#4iYQz}vX`ej~MyK9ru$SG*cu0o55<UsZ<)Pr(D>"
    "2E{M!C74B7o48nlS12Rh09EPSAv(Er(@KcK0v8`Qa~q`psv5`jg9_eCv}R{OtKpe{icoUTX^X7V|meb53RiNBWTI2<jCjFPx$&VX"
    "vgqGfMma^4(hvT0M(;gWz7+onAOdw4ISkGIC?G7+qN`kKvHPa<pgM&Fz2sH{W~ySHDkb<dwyVd8K`;fz?g0X|xD+=(DiRhg`Qp+~"
    "Uxi%%tC?{pWxG4@Bjx+-lG-?sbpnrTtHS|L-q9`jJ;Yo)-"
    "Zi?2Cx&$g3Hg&61t&bN4^}t?ys{=r1_Q(KUH}ax<9D<U;Gq{^eKy_S3I@qjCAsx1N3XW3LP^*C735c7n8*L6LXsKx)Lu0*&W?`9B"
    "DZM*JUp9MP<>fB7eWCXXY$yuvtAn9dY?k<jZ8e|Gu7?>+nBcf9h8Yta1XACWHn=r5jq_>ous`9~WjxSOOq4#T-"
    "YaNA@t9Ji7GYheD8gt2T512GJTB!BlkgD7@sq)^abe(MjOef)u=u$Q0tZ&H;Ad;D=Sp8(hVJ04M_tN"
)


def generate_threejs_html(track_data, title="3D Track Viewer", theme=None,
                          initial_field="elevation"):
    """Build the complete embedded QGIS 3D viewer and inject layer data."""
    import os
    import zlib

    plugin_dir = os.path.dirname(__file__)
    html_template = zlib.decompress(
        base64.b85decode(_PLUGIN_HTML_B85.encode("ascii"))
    ).decode("utf-8")

    # Settings are exposed only through the native toolbar. Remove the legacy
    # web-sidebar button and its attempts to reveal it when a track is loaded.
    html_template = re.sub(
        r'<button\b[^>]*\bid=["\']settings-btn["\'][^>]*>.*?</button>',
        '',
        html_template,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html_template = re.sub(
        r'document\.getElementById\(["\']settings-btn["\']\)'
        r'\.style\.display\s*=\s*["\']flex["\'];?',
        '',
        html_template,
    )

    # Use the largest cube that remains at least 2 px inside its 104 px canvas
    # for every possible 3D orientation. The worst projected half-extent is
    # halfSize * sqrt(3), so (52 - 2) / sqrt(3) gives about 28.87 px.
    html_template = html_template.replace(
        "var cx = 52, cy = 52, halfSize = 25;",
        "var cx = 52, cy = 52, halfSize = 28.86;",
    )
    orientation_spacing_rule = (
        "\n        #azimuth-input { top: 106px !important; }\n"
        "        #elevation-input { left: 106px !important; }\n"
        "        #tooltip strong { font-size: 10px !important; "
        "font-weight: bold !important; }\n"
        "        #tooltip table, #tooltip td { font-size: 10px !important; "
        "font-weight: normal !important; }\n"
    )
    if "</style>" not in html_template:
        raise ValueError("Missing style block in embedded viewer template")
    html_template = html_template.replace(
        "</style>", orientation_spacing_rule + "    </style>", 1)

    # With an empty keyframe list, Play should still provide a useful default
    # timeline animation.  Snapshot the current camera at both clipping bounds;
    # the track then progresses from the selected start to end while preserving
    # the user's current view.  Existing user keyframes are never modified.
    play_animation_pattern = (
        r'(        function playKeyframeAnimation\(\) \{\r?\n)'
        r'(            var frames = activeAnimationFrames\(\);)'
    )
    play_animation_replacement = (
        r"\1"
        "            if (animationKeyframes.length === 0 && "
        "timeControlAvailable && animationStateReader && "
        "clipEndIndex > clipStartIndex) {\n"
        "                var defaultCameraState = cloneAnimationState(animationStateReader());\n"
        "                animationKeyframes = [\n"
        "                    {timeIndex:clipStartIndex, state:cloneAnimationState(defaultCameraState), duration:2},\n"
        "                    {timeIndex:clipEndIndex, state:cloneAnimationState(defaultCameraState), duration:2}\n"
        "                ];\n"
        "                animationElapsed = 0;\n"
        "                refreshAnimationPanel();\n"
        "            }\n"
        r"            var frames = activeAnimationFrames();"
    )
    html_template, play_animation_count = re.subn(
        play_animation_pattern,
        play_animation_replacement,
        html_template,
        count=1,
    )
    if play_animation_count != 1:
        raise ValueError("Missing embedded keyframe playback logic")

    # The embedded QGIS document and the standalone site deliberately have
    # separate controls.  Older copies of the compressed renderer template
    # still contain the browser-only toolbar; remove it at generation time so
    # the QGIS dialog exposes only its native Qt toolbar.
    html_template, removed_toolbar_count = re.subn(
        r'\s*<!-- Standalone Web App Toolbar -->\s*'
        r'<div id="web-toolbar".*?</div>',
        "",
        html_template,
        count=1,
        flags=re.DOTALL,
    )
    if removed_toolbar_count != 1:
        raise ValueError("Missing standalone web toolbar in embedded template")

    # In normal clipping mode the visible geometry must follow both clipping
    # handles directly.  The separate playhead range is reserved for keyframe
    # preview and animation playback.
    new_visible_range = """            var clippingActive = document.getElementById('time-range-control').classList.contains('active');
            var animationEditing = !document.getElementById('animation-panel').classList.contains('hidden');
            var rangeStart = clippingActive && !animationEditing ? clipStartIndex : timeStartIndex;
            var rangeEnd = clippingActive && !animationEditing ? clipEndIndex : timeEndIndex;
            var start = Math.max(0, Math.min(rangeStart, rangeEnd));
            var end = Math.min(allTrackSegments.length - 1, Math.max(rangeStart, rangeEnd));
            return allTrackSegments.slice(start, end + 1);"""
    visible_range_pattern = (
        r'            var start = Math\.max\(0, Math\.min\(timeStartIndex, timeEndIndex\)\);\r?\n'
        r'            var end = Math\.min\(allTrackSegments\.length - 1, Math\.max\(timeStartIndex, timeEndIndex\)\);\r?\n'
        r'            return allTrackSegments\.slice\(start, end \+ 1\);'
    )
    html_template, replaced_range_count = re.subn(
        visible_range_pattern, new_visible_range, html_template, count=1)
    if replaced_range_count != 1:
        raise ValueError("Missing embedded time-clipping range logic")

    vendor_dir = os.path.join(plugin_dir, "vendor")
    vendor_scripts = (
        ("https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js",
         "three.min.js"),
        ("https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js",
         "OrbitControls.js"),
    )
    for remote_url, filename in vendor_scripts:
        vendor_path = os.path.join(vendor_dir, filename)
        with open(vendor_path, "r", encoding="utf-8") as vendor_file:
            source = vendor_file.read()
        if "</script" in source.lower():
            raise ValueError("Unsafe script terminator in bundled dependency")
        remote_tag = f'<script src="{remote_url}"></script>'
        if remote_tag not in html_template:
            raise ValueError(f"Missing dependency placeholder for {filename}")
        html_template = html_template.replace(
            remote_tag, f"<script>\n{source}\n</script>", 1)

    def safe_json(value):
        """Serialize JSON without allowing it to terminate the script node."""
        return (json.dumps(value, ensure_ascii=True, allow_nan=False)
                .replace("&", "\\u0026")
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))

    # Base64 encode icons for QtWebView local access safety
    interaction_icon_dir = os.path.join(plugin_dir, "icons_components")
    def embedded_svg_url(filename):
        path = os.path.join(interaction_icon_dir, filename)
        try:
            with open(path, "rb") as icon_file:
                payload = base64.b64encode(icon_file.read()).decode("ascii")
            return "data:image/svg+xml;base64," + payload
        except OSError:
            from qgis.PyQt.QtCore import QUrl
            return QUrl.fromLocalFile(path).toString()
            
    rotation_icon_url = embedded_svg_url("View_Rotation.svg")
    movement_icon_url = embedded_svg_url("Mobile_View.svg")
    collapse_icon_url = embedded_svg_url("Collapse.svg")
    expand_icon_url = embedded_svg_url("Expand.svg")
    
    json_data = safe_json(track_data)
    settings = QgsSettings()
    hover_options = {
        "show_time": settings.value("3DTrackViewer/hover_show_time", True, type=bool),
        "show_elevation": settings.value("3DTrackViewer/hover_show_elevation", True, type=bool),
        "show_speed": settings.value("3DTrackViewer/hover_show_speed", True, type=bool),
        "show_slope": settings.value("3DTrackViewer/hover_show_slope", True, type=bool),
        "unit_elevation": settings.value("3DTrackViewer/hover_unit_elevation", "m", type=str),
        "unit_speed": settings.value("3DTrackViewer/hover_unit_speed", "km/h", type=str),
        "unit_slope": settings.value("3DTrackViewer/hover_unit_slope", "%", type=str),
    }
    hover_json = safe_json(hover_options)
    theme_json = safe_json(theme or {})
    
    # We will build a replacement script to inject
    injection_script = f"""
        // --- QGIS DATA INJECTION START ---
        window.embeddedTrackData = {json_data};
        window.embeddedHoverOptions = {hover_json};
        window.embeddedInteractionIcons = {{
            rotate: {safe_json(rotation_icon_url)},
            move: {safe_json(movement_icon_url)}
        }};
        window.embeddedSidebarIcons = {{
            collapse: {safe_json(collapse_icon_url)},
            expand: {safe_json(expand_icon_url)}
        }};
        window.embeddedTheme = {theme_json};
        window.embeddedInitialField = {safe_json(initial_field)};
        // --- QGIS DATA INJECTION END ---
    """
    
    # Replace the complete placeholder block independently of whether the
    # compressed template uses LF or CRLF.  A plain string replacement used to
    # fail silently on Windows, causing the plugin document to start in the
    # standalone site's file-upload mode even though QGIS had already supplied
    # the selected layer data.
    injection_pattern = (
        r'        // --- QGIS DATA INJECTION START ---\r?\n'
        r'        window\.embeddedTrackData = null;\r?\n'
        r'        window\.embeddedHoverOptions = null;\r?\n'
        r'        window\.embeddedInteractionIcons = null;\r?\n'
        r'        window\.embeddedSidebarIcons = null;\r?\n'
        r'        window\.embeddedTheme = null;\r?\n'
        r'        // --- QGIS DATA INJECTION END ---'
    )
    injected_html, injection_count = re.subn(
        injection_pattern,
        lambda _match: injection_script,
        html_template,
        count=1,
    )
    if injection_count != 1:
        raise ValueError("Unable to inject QGIS track data into viewer template")
    if "window.embeddedTrackData = null;" in injected_html:
        raise ValueError("QGIS track data placeholder remains after injection")
    safe_title = html.escape(str(title), quote=True)
    injected_html = injected_html.replace(
        "<title>3D Track Viewer</title>", f"<title>{safe_title}</title>")
    
    return injected_html


def qgis_theme_colors(widget):
    """Return CSS colors derived from the active QGIS/Qt palette."""
    palette = widget.palette()
    font = widget.font()

    try:
        active_group = QPalette.Active
        roles = QPalette
    except AttributeError:
        active_group = QPalette.ColorGroup.Active
        roles = QPalette.ColorRole

    def color(role):
        return palette.color(active_group, role).name()

    return {
        "window": color(roles.Window),
        "base": color(roles.Base),
        "text": color(roles.WindowText),
        "muted": color(roles.Mid),
        "border": color(roles.Mid),
        "button": color(roles.Button),
        "highlight": color(roles.Highlight),
        "highlight_text": color(roles.HighlightedText),
        "grid": color(roles.Mid),
        "font_family": font.family().replace('"', ''),
        "font_size": font.pointSizeF() if font.pointSizeF() > 0 else 9,
    }


class TrackViewerUnitSettingsDialog(QDialog):
    """Native hover/unit settings, following ColorGPX's dialog pattern."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("3D Track Viewer Settings"))
        self.setMinimumWidth(180)
        self.setMaximumWidth(200)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        title_font = self.font()
        title_font.setPointSize(8)
        title_font.setBold(True)

        item_font = self.font()
        item_font.setPointSize(7)
        item_font.setBold(False)

        group = QGroupBox(tr("Hover display and units"), self)
        group.setFont(title_font)
        grid = QGridLayout(group)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setHorizontalSpacing(3)
        grid.setVerticalSpacing(2)

        self.show_time = QCheckBox(tr("Time"), group)
        self.show_time.setFont(item_font)
        self.show_elevation = QCheckBox(tr("Elevation"), group)
        self.show_elevation.setFont(item_font)
        self.show_speed = QCheckBox(tr("Speed"), group)
        self.show_speed.setFont(item_font)
        self.show_slope = QCheckBox(tr("Slope"), group)
        self.show_slope.setFont(item_font)

        self.elevation_unit = QComboBox(group)
        self.elevation_unit.setFont(item_font)
        self.elevation_unit.addItems(["m", "ft"])
        self.elevation_unit.setMaximumWidth(55)

        self.speed_unit = QComboBox(group)
        self.speed_unit.setFont(item_font)
        self.speed_unit.addItems(["km/h", "m/s", "mph"])
        self.speed_unit.setMaximumWidth(55)

        self.slope_unit = QComboBox(group)
        self.slope_unit.setFont(item_font)
        self.slope_unit.addItems(["%", "°"])
        self.slope_unit.setMaximumWidth(55)
        grid.addWidget(self.show_time, 0, 0, 1, 2)
        grid.addWidget(self.show_elevation, 1, 0)
        grid.addWidget(self.elevation_unit, 1, 1)
        grid.addWidget(self.show_speed, 2, 0)
        grid.addWidget(self.speed_unit, 2, 1)
        grid.addWidget(self.show_slope, 3, 0)
        grid.addWidget(self.slope_unit, 3, 1)
        self.show_elevation.toggled.connect(self.elevation_unit.setEnabled)
        self.show_speed.toggled.connect(self.speed_unit.setEnabled)
        self.show_slope.toggled.connect(self.slope_unit.setEnabled)
        layout.addWidget(group)

        buttons = QHBoxLayout()
        buttons.addStretch()
        ok_button = QPushButton(tr("OK"), self)
        cancel_button = QPushButton(tr("Cancel"), self)
        ok_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(ok_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)
        self._load()

    def _load(self):
        settings = QgsSettings()
        self.show_time.setChecked(settings.value(
            "3DTrackViewer/hover_show_time", True, type=bool))
        self.show_elevation.setChecked(settings.value(
            "3DTrackViewer/hover_show_elevation", True, type=bool))
        self.show_speed.setChecked(settings.value(
            "3DTrackViewer/hover_show_speed", True, type=bool))
        self.show_slope.setChecked(settings.value(
            "3DTrackViewer/hover_show_slope", True, type=bool))
        self.elevation_unit.setCurrentText(settings.value(
            "3DTrackViewer/hover_unit_elevation", "m", type=str))
        self.speed_unit.setCurrentText(settings.value(
            "3DTrackViewer/hover_unit_speed", "km/h", type=str))
        self.slope_unit.setCurrentText(settings.value(
            "3DTrackViewer/hover_unit_slope", "%", type=str))

    def values(self):
        return {
            "show_time": self.show_time.isChecked(),
            "show_elevation": self.show_elevation.isChecked(),
            "show_speed": self.show_speed.isChecked(),
            "show_slope": self.show_slope.isChecked(),
            "unit_elevation": self.elevation_unit.currentText(),
            "unit_speed": self.speed_unit.currentText(),
            "unit_slope": self.slope_unit.currentText(),
        }


class GifExportSettingsDialog(QDialog):
    """Collect GIF timing settings and preview the derived output size."""

    def __init__(self, base_duration, parent=None):
        super().__init__(parent)
        self.base_duration = max(0.1, float(base_duration))
        self.setWindowTitle(tr("Export GIF"))
        self.setMinimumWidth(180)
        self.setMaximumWidth(200)

        font = self.font()
        font.setPointSize(8)
        self.setFont(font)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        form = QFormLayout()
        form.setContentsMargins(4, 4, 4, 4)
        form.setHorizontalSpacing(4)
        form.setVerticalSpacing(4)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.speed = QDoubleSpinBox(self)
        self.speed.setRange(0.25, 20.0)
        self.speed.setDecimals(2)
        self.speed.setSingleStep(0.25)
        self.speed.setPrefix("x")
        self.speed.setValue(2.0)
        self.speed.setMaximumWidth(80)
        form.addRow(tr("Animation speed:"), self.speed)

        self.fps = QSpinBox(self)
        self.fps.setRange(1, 60)
        self.fps.setValue(15)
        self.fps.setSuffix(" FPS")
        self.fps.setMaximumWidth(80)
        form.addRow(tr("Frames per second:"), self.fps)

        self.duration_label = QLabel(self)
        self.frame_count_label = QLabel(self)
        form.addRow(tr("Animation duration:"), self.duration_label)
        form.addRow(tr("Total frames:"), self.frame_count_label)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        export_button = QPushButton(tr("Export"), self)
        cancel_button = QPushButton(tr("Cancel"), self)
        export_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(export_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

        self.speed.valueChanged.connect(self._update_summary)
        self.fps.valueChanged.connect(self._update_summary)
        self._update_summary()

    def export_duration(self):
        return self.base_duration / self.speed.value()

    def frame_count(self):
        return max(2, int(round(self.export_duration() * self.fps.value())))

    def _update_summary(self, _value=None):
        duration = self.export_duration()
        self.duration_label.setText(tr("{:.1f} seconds").format(duration))
        self.frame_count_label.setText(tr("{} frames").format(self.frame_count()))


class ImageExportSettingsDialog(QDialog):
    """Select the raster format and output pixel dimensions."""

    FORMATS = {
        "PNG": (".png", "PNG Images (*.png)"),
        "JPEG": (".jpg", "JPEG Images (*.jpg *.jpeg)"),
        "BMP": (".bmp", "BMP Images (*.bmp)"),
    }

    def __init__(self, width, height, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Export 3D Image"))
        self.setMinimumWidth(180)
        self.setMaximumWidth(200)
        self._aspect_ratio = max(1, int(width)) / float(max(1, int(height)))
        self._updating_size = False

        font = self.font()
        font.setPointSize(8)
        self.setFont(font)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        form = QFormLayout()
        form.setContentsMargins(4, 4, 4, 4)
        form.setHorizontalSpacing(4)
        form.setVerticalSpacing(4)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.image_format = QComboBox(self)
        self.image_format.addItems(self.FORMATS.keys())
        self.image_format.setMaximumWidth(80)
        form.addRow(tr("Image format:"), self.image_format)

        self.width = QSpinBox(self)
        self.width.setRange(64, 16384)
        self.width.setValue(max(64, min(16384, int(width))))
        self.width.setSuffix(" px")
        self.width.setMaximumWidth(80)
        form.addRow(tr("Width:"), self.width)

        self.height = QSpinBox(self)
        self.height.setRange(64, 16384)
        self.height.setValue(max(64, min(16384, int(height))))
        self.height.setSuffix(" px")
        self.height.setMaximumWidth(80)
        form.addRow(tr("Height:"), self.height)

        self.keep_aspect = QCheckBox(tr("Keep aspect ratio"), self)
        self.keep_aspect.setChecked(True)
        form.addRow("", self.keep_aspect)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        export_button = QPushButton(tr("Export"), self)
        cancel_button = QPushButton(tr("Cancel"), self)
        export_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(export_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

        self.width.valueChanged.connect(self._width_changed)
        self.height.valueChanged.connect(self._height_changed)

    def _width_changed(self, value):
        if self.keep_aspect.isChecked() and not self._updating_size:
            self._updating_size = True
            self.height.setValue(max(64, min(16384, int(round(value / self._aspect_ratio)))))
            self._updating_size = False

    def _height_changed(self, value):
        if self.keep_aspect.isChecked() and not self._updating_size:
            self._updating_size = True
            self.width.setValue(max(64, min(16384, int(round(value * self._aspect_ratio)))))
            self._updating_size = False

    def selected_format(self):
        return self.image_format.currentText()


class TrackViewer3DDialog(QDialog):
    """
    Embedded PyQt dialog containing a QGIS-themed 3D view.
    """
    def __init__(self, html_path, title="3D Track Viewer", parent=None,
                 layer=None, iface=None):
        super().__init__(parent)
        self.html_path = html_path
        self.layer = layer
        self.iface = iface
        self.setAcceptDrops(True)
        try:
            window_controls = (Qt.WindowSystemMenuHint |
                               Qt.WindowMinimizeButtonHint |
                               Qt.WindowMaximizeButtonHint |
                               Qt.WindowCloseButtonHint)
        except AttributeError:  # PyQt6 / QGIS 4 scoped enums
            window_controls = (Qt.WindowType.WindowSystemMenuHint |
                               Qt.WindowType.WindowMinimizeButtonHint |
                               Qt.WindowType.WindowMaximizeButtonHint |
                               Qt.WindowType.WindowCloseButtonHint)
        self.setWindowFlags(self.windowFlags() | window_controls)
        self.setWindowTitle(title)
        self.resize(820, 540)
        self.setMinimumSize(700, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Native toolbar inherits the active QGIS theme.
        toolbar = QFrame(self)
        toolbar.setFrameShape(QFrame.StyledPanel)
        toolbar.setMaximumHeight(40)
        top_bar = QHBoxLayout(toolbar)
        top_bar.setContentsMargins(8, 3, 8, 3)
        top_bar.setSpacing(5)
        title_label = QLabel(f"<b>{tr('3D Track')}</b>")
        top_bar.addWidget(title_label)

        self.layer_combo = QComboBox(toolbar)
        self.layer_combo.setToolTip(tr("Switch currently displayed track layer"))
        self.layer_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.layer_combo.setFixedWidth(200)
        self._populate_layer_combo()
        self.layer_combo.currentIndexChanged.connect(self._on_layer_changed)
        top_bar.addWidget(self.layer_combo)

        icon_dir = os.path.join(os.path.dirname(__file__), "icons_components")

        self.open_gpx_button = QPushButton(toolbar)
        self.open_gpx_button.setIcon(
            QIcon(os.path.join(icon_dir, "Open_GPX.svg")))
        self.open_gpx_button.setIconSize(QSize(20, 20))
        self.open_gpx_button.setToolTip(tr("Open another local GPX file"))
        self.open_gpx_button.setAccessibleName(tr("Open GPX File"))
        self.open_gpx_button.setFixedSize(28, 28)
        self.open_gpx_button.clicked.connect(self._choose_gpx_file)
        top_bar.addWidget(self.open_gpx_button)

        self.view_control_buttons = []
        for icon_name, tooltip, script in (
            ("Highly_adaptive.svg", tr("Height Auto-fit"), "fitZScale();"),
            ("Scale_to_trajectory.svg", tr("Zoom to Layer"), "zoomToVisibleLayer();"),
            ("Reset_Perspective.svg", tr("Reset View"), "resetActiveCamera();"),
        ):
            button = QPushButton(toolbar)
            button.setIcon(QIcon(os.path.join(icon_dir, icon_name)))
            button.setIconSize(QSize(20, 20))
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.setFixedSize(28, 28)
            try:
                button.setFocusPolicy(Qt.NoFocus)
            except AttributeError:
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, command=script: self._invoke_view_command(command))
            top_bar.addWidget(button)
            self.view_control_buttons.append(button)

        self.time_clip_button = QPushButton(toolbar)
        self.time_clip_button.setIcon(QIcon(os.path.join(icon_dir, "Trajectory_clipping.svg")))
        self.time_clip_button.setIconSize(QSize(20, 20))
        self.time_clip_button.setToolTip(tr("Track Time Clipping"))
        self.time_clip_button.setAccessibleName(tr("Track Time Clipping"))
        self.time_clip_button.setCheckable(True)
        self.time_clip_button.setFixedSize(28, 28)
        self.time_clip_button.setEnabled(False)
        self.time_clip_button.toggled.connect(self._toggle_time_clipping)
        top_bar.addWidget(self.time_clip_button)
        self.view_control_buttons.append(self.time_clip_button)

        self.animation_button = QPushButton(toolbar)
        self.animation_button.setIcon(QIcon(os.path.join(icon_dir, "key_frame.svg")))
        self.animation_button.setIconSize(QSize(20, 20))
        self.animation_button.setToolTip(tr("Keyframe Animation"))
        self.animation_button.setAccessibleName(tr("Keyframe Animation"))
        self.animation_button.setCheckable(True)
        self.animation_button.setFixedSize(28, 28)
        self.animation_button.setEnabled(False)
        self.animation_button.toggled.connect(self._toggle_animation_panel)
        top_bar.addWidget(self.animation_button)
        self.view_control_buttons.append(self.animation_button)

        self.follow_button = QPushButton(toolbar)
        self.follow_button.setIcon(QIcon(os.path.join(icon_dir, "follow_display.svg")))
        self.follow_button.setIconSize(QSize(20, 20))
        self.follow_button.setToolTip(tr("Follow Display: Show time, elevation, speed, and slope along track"))
        self.follow_button.setAccessibleName(tr("Follow Display"))
        self.follow_button.setCheckable(True)
        self.follow_button.setFixedSize(28, 28)
        self.follow_button.setEnabled(False)
        self.follow_button.toggled.connect(self._toggle_follow_display)
        top_bar.addWidget(self.follow_button)

        self.settings_button = QPushButton(toolbar)
        self.settings_button.setIcon(QIcon(os.path.join(icon_dir, "Setting.svg")))
        self.settings_button.setIconSize(QSize(20, 20))
        self.settings_button.setToolTip(tr("Unit and hover settings"))
        self.settings_button.setAccessibleName(tr("Settings"))
        self.settings_button.setFixedSize(28, 28)
        self.settings_button.clicked.connect(self._show_unit_settings)
        top_bar.addWidget(self.settings_button)

        top_bar.addStretch()

        export_buttons = (
            ("export_image.svg", tr("Export Image"), self._export_image),
            ("export_svg.svg", tr("Export SVG"), self._export_svg),
            ("export_pdf.svg", tr("Export PDF"), self._export_pdf),
        )
        for icon_name, tooltip, handler in export_buttons:
            button = QPushButton(toolbar)
            button.setIcon(QIcon(os.path.join(icon_dir, icon_name)))
            button.setIconSize(QSize(20, 20))
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.setFixedSize(28, 28)
            button.setEnabled(HAS_WEBVIEW and bool(html_path))
            button.clicked.connect(handler)
            top_bar.addWidget(button)

        self.gif_button = QPushButton(toolbar)
        self.gif_button.setIcon(QIcon(os.path.join(icon_dir, "export_gif.svg")))
        self.gif_button.setIconSize(QSize(20, 20))
        self.gif_button.setToolTip(tr("Export Keyframe Animation as GIF"))
        self.gif_button.setAccessibleName(tr("Export GIF"))
        self.gif_button.setFixedSize(28, 28)
        self.gif_button.setEnabled(False)
        self.gif_button.clicked.connect(self._export_gif)
        top_bar.addWidget(self.gif_button)

        btn_browser = QPushButton(toolbar)
        btn_browser.setIcon(QIcon(os.path.join(icon_dir, "open_browser.svg")))
        btn_browser.setIconSize(QSize(20, 20))
        btn_browser.setToolTip(tr("Open Full 3D View in System Browser"))
        btn_browser.setAccessibleName(tr("Open in Browser"))
        btn_browser.setFixedSize(28, 28)
        btn_browser.setEnabled(bool(html_path))
        btn_browser.clicked.connect(self._open_external)
        top_bar.addWidget(btn_browser)

        # A QGIS project layer is already imported. Show this action only for
        # a viewer created from a local GPX, to the right of browser opening.
        self.import_button = QPushButton(toolbar)
        export_qgis_icon = os.path.join(icon_dir, "Export_to_QGIS.svg")
        if os.path.exists(export_qgis_icon):
            white_pixmap = QPixmap(export_qgis_icon)
            import_icon = QIcon()
            try:
                icon_modes = (QIcon.Normal, QIcon.Disabled,
                              QIcon.Active, QIcon.Selected)
                icon_off, icon_on = QIcon.Off, QIcon.On
            except AttributeError:
                icon_modes = (QIcon.Mode.Normal, QIcon.Mode.Disabled,
                              QIcon.Mode.Active, QIcon.Mode.Selected)
                icon_off, icon_on = QIcon.State.Off, QIcon.State.On
            for mode in icon_modes:
                import_icon.addPixmap(white_pixmap, mode, icon_off)
                import_icon.addPixmap(white_pixmap, mode, icon_on)
            self.import_button.setIcon(import_icon)
        self.import_button.setIconSize(QSize(20, 20))
        self.import_button.setToolTip(
            tr("Import this temporary GPX layer into your QGIS project layers list"))
        self.import_button.setAccessibleName(tr("Import to QGIS"))
        self.import_button.setFixedSize(28, 28)
        self.import_button.setStyleSheet(
            "background-color: #4caf50; border-radius: 3px;")
        is_local_gpx = bool(
            self.layer and self.layer.customProperty("gpx_path", ""))
        self.import_button.setVisible(is_local_gpx)
        self.import_button.setEnabled(is_local_gpx)
        self.import_button.clicked.connect(self._import_layer_to_qgis)
        top_bar.addWidget(self.import_button)

        layout.addWidget(toolbar)

        # Web View
        if HAS_WEBVIEW and html_path:
            interaction_icon_dir = os.path.join(
                os.path.dirname(__file__), "icons_components")
            self.web_view = TrackViewerInteractionWebView(
                os.path.join(interaction_icon_dir, "View_Rotation.svg"),
                os.path.join(interaction_icon_dir, "Mobile_View.svg"),
                self,
            )
            self.web_view.gpx_drop_handler = self._load_gpx_path
            try:
                s = self.web_view.settings()
                settings_class = (QWebSettings if WEB_BACKEND == "webkit"
                                  else QWebEngineSettings)
                attributes = settings_class.WebAttribute
                s.setAttribute(attributes.WebGLEnabled, True)
                s.setAttribute(attributes.JavascriptEnabled, True)
                if hasattr(attributes, "AcceleratedCompositingEnabled"):
                    s.setAttribute(attributes.AcceleratedCompositingEnabled, True)
                elif hasattr(attributes, "Accelerated2dCanvasEnabled"):
                    s.setAttribute(attributes.Accelerated2dCanvasEnabled, True)
                s.setAttribute(attributes.LocalContentCanAccessRemoteUrls, False)
                s.setAttribute(attributes.LocalContentCanAccessFileUrls, False)
            except (AttributeError, RuntimeError) as error:
                QgsMessageLog.logMessage(
                    f"Unable to apply hardened web-view settings: {error}",
                    "3D Track Viewer", Qgis.Warning)
            self.web_view.loadFinished.connect(self._on_view_loaded)
            self.web_view.load(QUrl.fromLocalFile(html_path))
            layout.addWidget(self.web_view)
        elif html_path:
            info_label = QLabel(tr("Embedded 3D is not supported in current QGIS environment. Opened in system browser."))
            try:
                center_alignment = Qt.AlignCenter
            except AttributeError:
                center_alignment = Qt.AlignmentFlag.AlignCenter
            info_label.setAlignment(center_alignment)
            layout.addWidget(info_label)
        else:
            empty_label = QLabel(
                tr("Select a QGIS layer or open a local GPX file from the upper-left toolbar."))
            try:
                center_alignment = Qt.AlignCenter
            except AttributeError:
                center_alignment = Qt.AlignmentFlag.AlignCenter
            empty_label.setAlignment(center_alignment)
            empty_label.setStyleSheet("color: palette(mid); font-size: 12pt;")
            layout.addWidget(empty_label)

    def _open_external(self):
        if self.html_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.html_path))

    @staticmethod
    def _dialog_dropped_gpx_path(event):
        mime_data = event.mimeData()
        if not mime_data or not mime_data.hasUrls():
            return ""
        paths = [url.toLocalFile() for url in mime_data.urls()
                 if url.isLocalFile()]
        if len(paths) != 1 or not paths[0].lower().endswith(".gpx"):
            return ""
        return paths[0]

    def dragEnterEvent(self, event):
        if self._dialog_dropped_gpx_path(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._dialog_dropped_gpx_path(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        path = self._dialog_dropped_gpx_path(event)
        if path:
            event.acceptProposedAction()
            self._load_gpx_path(path)
            return
        super().dropEvent(event)

    def closeEvent(self, event):
        """Remove the generated track document when the viewer is closed."""
        try:
            if self.html_path and os.path.isfile(self.html_path):
                os.remove(self.html_path)
        except OSError as error:
            QgsMessageLog.logMessage(
                f"Unable to remove temporary viewer file: {error}",
                "3D Track Viewer", Qgis.Warning)
        super().closeEvent(event)

    def _on_view_loaded(self, ok):
        self.follow_button.setEnabled(bool(ok))
        self.gif_button.setEnabled(bool(ok))
        for button in getattr(self, "view_control_buttons", []):
            button.setEnabled(bool(ok))
        if ok and hasattr(self, "time_clip_button"):
            try:
                available = bool(self.web_view.evaluate_javascript(
                    "timeControlAvailable === true;"))
                self.time_clip_button.setEnabled(available)
                if not available:
                    self.time_clip_button.setToolTip(tr("Track Time Clipping: Current layer does not have sufficient time information"))
            except Exception:
                self.time_clip_button.setEnabled(False)
        if ok and self.follow_button.isChecked():
            self._toggle_follow_display(True)

    def _invoke_view_command(self, script):
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        try:
            self.web_view.evaluate_javascript(script)
        except (AttributeError, RuntimeError) as error:
            QgsMessageLog.logMessage(
                f"3D view command failed: {error}",
                "3D Track Viewer", Qgis.Warning)

    def _toggle_time_clipping(self, enabled):
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        try:
            if not enabled and hasattr(self, "animation_button") and self.animation_button.isChecked():
                self.animation_button.setChecked(False)
            result = self.web_view.evaluate_javascript(
                "setTimeClippingEnabled(%s);" % ("true" if enabled else "false"))
            if enabled and not bool(result):
                self.time_clip_button.blockSignals(True)
                self.time_clip_button.setChecked(False)
                self.time_clip_button.blockSignals(False)
        except Exception:
            self.time_clip_button.blockSignals(True)
            self.time_clip_button.setChecked(False)
            self.time_clip_button.blockSignals(False)

    def _toggle_follow_display(self, enabled):
        """Synchronize the native toolbar toggle with the HTML renderer."""
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        try:
            script = "setFollowDisplayEnabled(%s);" % ("true" if enabled else "false")
            self.web_view.evaluate_javascript(script)
        except Exception:
            self.follow_button.blockSignals(True)
            self.follow_button.setChecked(False)
            self.follow_button.blockSignals(False)

    def _show_unit_settings(self):
        """Show native settings and synchronize accepted values to the view."""
        dialog = TrackViewerUnitSettingsDialog(self)
        result = dialog.exec()
        try:
            accepted = QDialog.Accepted
        except AttributeError:
            accepted = QDialog.DialogCode.Accepted
        if result != accepted:
            return

        values = dialog.values()
        settings = QgsSettings()
        for key, value in values.items():
            settings.setValue(f"3DTrackViewer/hover_{key}", value)

        if HAS_WEBVIEW and hasattr(self, "web_view"):
            options_json = (json.dumps(values, ensure_ascii=True, allow_nan=False)
                            .replace("&", "\\u0026")
                            .replace("<", "\\u003c")
                            .replace(">", "\\u003e"))
            script = (
                f"hoverOptions={options_json};"
                "invalidateClippedColorRanges();updateColorRangeModeUi();"
                "if(trackRenderHandler)trackRenderHandler();")
            self._invoke_view_command(script)

    def _toggle_animation_panel(self, enabled):
        """Show or hide the keyframe editor inside the 3D viewport."""
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        try:
            if enabled and hasattr(self, "time_clip_button") and not self.time_clip_button.isChecked():
                self.time_clip_button.setChecked(True)
            self.web_view.evaluate_javascript(
                "toggleAnimationPanel(%s);" % ("true" if enabled else "false"))
        except Exception:
            self.animation_button.blockSignals(True)
            self.animation_button.setChecked(False)
            self.animation_button.blockSignals(False)

    def _export_path(self, caption, file_filter, extension):
        default_name = self.layer.name() if self.layer else "3DTrackViewer_3D"
        path, _ = QFileDialog.getSaveFileName(
            self, caption, f"{default_name}_3D{extension}", file_filter)
        if path and not path.lower().endswith(extension):
            path += extension
        return path

    def _viewport_pixmap(self):
        """Capture only the right-hand 3D viewport in device pixels."""
        pixmap = self.web_view.grab()
        try:
            raw_rect = self.web_view.evaluate_javascript(
                "getExportViewportRect();")
            rect = json.loads(str(raw_rect))
            widget_width = max(1, self.web_view.width())
            widget_height = max(1, self.web_view.height())
            scale_x = pixmap.width() / float(widget_width)
            scale_y = pixmap.height() / float(widget_height)
            x = max(0, int(round(float(rect["x"]) * scale_x)))
            y = max(0, int(round(float(rect["y"]) * scale_y)))
            width = min(pixmap.width() - x, int(round(float(rect["width"]) * scale_x)))
            height = min(pixmap.height() - y, int(round(float(rect["height"]) * scale_y)))
            if width > 0 and height > 0:
                return pixmap.copy(QRect(x, y, width, height))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return pixmap
        return pixmap

    def _export_image(self):
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        source = self._viewport_pixmap()
        dialog = ImageExportSettingsDialog(source.width(), source.height(), self)
        result = dialog.exec()
        try:
            accepted = QDialog.Accepted
        except AttributeError:
            accepted = QDialog.DialogCode.Accepted
        if result != accepted:
            return

        image_format = dialog.selected_format()
        extension, file_filter = dialog.FORMATS[image_format]
        path = self._export_path(
            tr("Export 3D Image"), tr(file_filter), extension)
        if not path:
            return
        try:
            ignore_aspect = Qt.IgnoreAspectRatio
            smooth = Qt.SmoothTransformation
        except AttributeError:
            ignore_aspect = Qt.AspectRatioMode.IgnoreAspectRatio
            smooth = Qt.TransformationMode.SmoothTransformation
        output = source.scaled(
            dialog.width.value(), dialog.height.value(),
            ignore_aspect, smooth)
        quality = 95 if image_format == "JPEG" else -1
        if not output.save(path, image_format, quality):
            QMessageBox.warning(self, tr("3D Track Viewer"), tr("Image export failed."))

    def _export_gif(self):
        """Render keyframes at fixed time steps and encode an animated GIF."""
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        try:
            from PIL import Image
        except ImportError:
            QMessageBox.warning(
                self, tr("3D Track Viewer"),
                tr("GIF export requires the Pillow Python package in the QGIS environment."))
            return

        try:
            raw_info = self.web_view.evaluate_javascript(
                "getAnimationExportInfo();")
            info = json.loads(str(raw_info))
        except (TypeError, ValueError, json.JSONDecodeError):
            QMessageBox.warning(self, tr("3D Track Viewer"), tr("Unable to read keyframe animation settings."))
            return
        if int(info.get("count", 0)) < 2 or float(info.get("baseDuration", 0)) <= 0:
            QMessageBox.information(
                self, tr("3D Track Viewer"),
                tr("Add at least two valid keyframes inside the clipping interval before exporting GIF."))
            return

        export_dialog = GifExportSettingsDialog(info.get("baseDuration", 0), self)
        result = export_dialog.exec()
        try:
            accepted = QDialog.Accepted
        except AttributeError:
            accepted = QDialog.DialogCode.Accepted
        if result != accepted:
            return
        speed = export_dialog.speed.value()
        fps = export_dialog.fps.value()
        export_duration = export_dialog.export_duration()
        # Include both endpoints while keeping the encoded GIF's total delay
        # equal to the requested duration (the final frame also has a delay).
        frame_count = export_dialog.frame_count()
        if frame_count > 450:
            QMessageBox.warning(
                self, tr("3D Track Viewer"),
                tr("GIF export exceeds the 450-frame safety limit. Increase playback speed or reduce FPS."))
            return
        estimated_memory = (
            max(1, self.web_view.width()) * max(1, self.web_view.height()) *
            3 * frame_count)
        if estimated_memory > MAX_GIF_MEMORY_BYTES:
            QMessageBox.warning(
                self, tr("3D Track Viewer"),
                tr("GIF export would exceed the 512 MB memory safety limit. "
                   "Reduce the window size, playback duration, or FPS."))
            return
        path = self._export_path(tr("Export 3D GIF"), tr("GIF Animations (*.gif)"), ".gif")
        if not path:
            return

        progress = QProgressDialog(tr("Rendering GIF frames..."), tr("Cancel"), 0, frame_count, self)
        progress.setWindowTitle(tr("Export GIF"))
        try:
            progress.setWindowModality(Qt.WindowModal)
        except AttributeError:
            progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        frames = []
        temporary_path = path + ".tmp"
        export_started = False
        try:
            export_started = bool(self.web_view.evaluate_javascript(
                "beginAnimationExport();"))
            if not export_started:
                raise RuntimeError(tr("Animation export could not be initialized."))
            for index in range(frame_count):
                if progress.wasCanceled():
                    break
                seconds = export_duration * index / float(frame_count - 1)
                # The web API applies the animation panel's current speed.
                # Compensate for it so this dialog's independent multiplier is
                # the one that controls the exported GIF.
                panel_speed = max(0.01, float(info.get("speed", 1.0)))
                web_seconds = seconds * speed / panel_speed
                self.web_view.evaluate_javascript(
                    "setAnimationExportTime(%.9f);" % web_seconds)
                QCoreApplication.processEvents()
                pixmap = self._viewport_pixmap()
                encoded = QByteArray()
                buffer = QBuffer(encoded)
                try:
                    write_only = QIODevice.WriteOnly
                except AttributeError:
                    write_only = QIODevice.OpenModeFlag.WriteOnly
                buffer.open(write_only)
                if not pixmap.save(buffer, "PNG"):
                    raise RuntimeError(tr("Unable to capture an animation frame."))
                buffer.close()
                with Image.open(io.BytesIO(bytes(encoded))) as image:
                    frames.append(image.convert("RGB").copy())
                progress.setValue(index + 1)
                QCoreApplication.processEvents()

            if progress.wasCanceled():
                return
            if not frames:
                raise RuntimeError(tr("No animation frames were generated."))
            save_options = {
                "format": "GIF", "save_all": True,
                "append_images": frames[1:],
                "duration": max(20, int(round(export_duration * 1000.0 / frame_count))),
                "optimize": True
            }
            if bool(info.get("loop", False)):
                save_options["loop"] = 0
            frames[0].save(temporary_path, **save_options)
            os.replace(temporary_path, path)
            progress.setValue(frame_count)
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, tr("3D Track Viewer"), f"{tr('GIF export failed:')} {error}")
        finally:
            if export_started:
                try:
                    self.web_view.evaluate_javascript("endAnimationExport();")
                except (AttributeError, RuntimeError) as error:
                    QgsMessageLog.logMessage(
                        f"Unable to finalize animation export: {error}",
                        "3D Track Viewer", Qgis.Warning)
            progress.close()
            if os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError as error:
                    QgsMessageLog.logMessage(
                        f"Unable to remove temporary GIF: {error}",
                        "3D Track Viewer", Qgis.Warning)

    def _export_svg(self):
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        path = self._export_path(tr("Export 3D SVG"), tr("SVG Vector Graphics (*.svg)"), ".svg")
        if not path:
            return
        svg = self._vector_svg()
        if not svg:
            QMessageBox.warning(self, tr("3D Track Viewer"), tr("Vector scene unavailable. Please wait for 3D view to load and try again."))
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as output:
                output.write(svg)
        except (OSError, UnicodeError) as error:
            QMessageBox.warning(self, tr("3D Track Viewer"), f"{tr('SVG export failed:')} {error}")

    def _export_pdf(self):
        if not HAS_WEBVIEW or not hasattr(self, "web_view"):
            return
        path = self._export_path(tr("Export 3D PDF"), tr("PDF Documents (*.pdf)"), ".pdf")
        if not path:
            return
        svg = self._vector_svg()
        if not svg:
            QMessageBox.warning(self, tr("3D Track Viewer"), tr("Vector scene unavailable. Please wait for 3D view to load and try again."))
            return
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not renderer.isValid():
            QMessageBox.warning(self, tr("3D Track Viewer"), tr("Invalid vector scene, PDF export canceled."))
            return
        try:
            printer = QPrinter(QPrinter.HighResolution)
            pdf_format = QPrinter.PdfFormat
        except AttributeError:  # PyQt6 / QGIS 4 scoped enums
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            pdf_format = QPrinter.OutputFormat.PdfFormat
        printer.setOutputFormat(pdf_format)
        printer.setOutputFileName(path)
        try:
            printer.setOrientation(QPrinter.Landscape)
        except AttributeError:  # PyQt6 / QGIS 4 scoped enums
            printer.setPageOrientation(QPageLayout.Orientation.Landscape)
        painter = QPainter(printer)
        if not painter.isActive():
            QMessageBox.warning(self, "3D Track Viewer", tr("Unable to create PDF painter device."))
            return
        renderer.render(painter, QRectF(painter.viewport()))
        painter.end()

    def _vector_svg(self):
        """Return an editable SVG scene instead of rasterizing the web view."""
        try:
            value = self.web_view.evaluate_javascript("buildVectorSvg();")
            svg = str(value) if value is not None else ""
        except Exception:
            return ""
        return svg if svg.lstrip().startswith("<?xml") and "<svg" in svg else ""

    def _populate_layer_combo(self):
        """List usable point/line layers and select the displayed layer."""
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        current_id = self.layer.id() if self.layer else ""
        if not self.layer:
            self.layer_combo.addItem(tr("Select QGIS layer..."), "")
        candidates = []
        for candidate in QgsProject.instance().mapLayers().values():
            if not candidate or not candidate.isValid() or not hasattr(candidate, "geometryType"):
                continue
            if candidate.geometryType() not in (Qgis.GeometryType.Point, Qgis.GeometryType.Line):
                continue
            candidates.append(candidate)

        # If current layer is a temporary/in-memory layer not in project, add it too
        if self.layer and self.layer.id() not in QgsProject.instance().mapLayers():
            candidates.append(self.layer)

        for candidate in sorted(candidates, key=lambda item: item.name().lower()):
            self.layer_combo.addItem(candidate.name(), candidate.id())
            if candidate.id() == current_id:
                self.layer_combo.setCurrentIndex(self.layer_combo.count() - 1)
        self.layer_combo.blockSignals(False)

    def _on_layer_changed(self, index):
        """Rebuild the viewer for the layer selected in the toolbar."""
        if index < 0 or not self.iface:
            return
        layer_id = self.layer_combo.itemData(index)
        if not layer_id:
            return
        if self.layer and layer_id == self.layer.id():
            return
        selected_layer = QgsProject.instance().mapLayer(layer_id)
        # Fallback to current temporary layer if switching back to it
        if not selected_layer and self.layer and layer_id == self.layer.id():
            selected_layer = self.layer
        if not selected_layer:
            return

        # Reuse the normal extraction path so switching and have identical behavior.
        previous_dialog = self
        open_threejs_3d_view(selected_layer, self.iface)
        if _active_3d_dialog is not previous_dialog:
            previous_dialog.accept()
        else:
            # Extraction failed; keep current viewer
            self.layer_combo.blockSignals(True)
            current_index = self.layer_combo.findData(self.layer.id()) if self.layer else -1
            if current_index >= 0:
                self.layer_combo.setCurrentIndex(current_index)
            self.layer_combo.blockSignals(False)

    def _choose_gpx_file(self):
        """Choose a GPX through the native QGIS dialog, never through HTML."""
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, tr("Select GPX File"), "", tr("GPX Files (*.gpx)"))
        if path:
            self._load_gpx_path(path)

    def _load_gpx_path(self, path):
        """Validate a local GPX and reopen the viewer with its track data."""
        path = os.path.abspath(os.path.normpath(str(path)))
        if not path.lower().endswith(".gpx") or not os.path.isfile(path):
            QMessageBox.warning(
                self, tr("Error"), tr("Please select an existing GPX file."))
            return
        try:
            file_size = os.path.getsize(path)
        except OSError as error:
            QMessageBox.critical(
                self, tr("Error"),
                tr("Unable to inspect the selected GPX file: {}").format(error))
            return
        if file_size > MAX_GPX_FILE_BYTES:
            QMessageBox.warning(
                self, tr("GPX file is too large"),
                tr("The selected GPX file exceeds the 100 MB safety limit."))
            return

        layer_name = os.path.splitext(os.path.basename(path))[0]
        loaded_layer = None
        for sublayer in ("track_points", "tracks"):
            candidate = QgsVectorLayer(
                f"{path}|layername={sublayer}", layer_name, "ogr")
            if candidate.isValid() and candidate.featureCount() > 0:
                loaded_layer = candidate
                break
        if loaded_layer is None:
            QMessageBox.critical(
                self, tr("Error"),
                tr("Failed to read the GPX file as track geometry."))
            return

        loaded_layer.setCustomProperty("gpx_path", path)
        previous_dialog = self
        open_threejs_3d_view(loaded_layer, self.iface)
        if _active_3d_dialog is not previous_dialog:
            previous_dialog.accept()

    def _import_layer_to_qgis(self):
        """Import the temporary loaded layer into QGIS layers list."""
        if not self.layer:
            return
            
        gpx_path = self.layer.customProperty("gpx_path")
        if gpx_path:
            # Probe the conventional OGR GPX sublayers and offer only layers
            # that actually exist and contain features in this file.
            layer_name = os.path.splitext(os.path.basename(gpx_path))[0]
            available_layers = []
            for sublayer_name in (
                    "track_points", "tracks", "routes", "waypoints"):
                candidate = QgsVectorLayer(
                    f"{gpx_path}|layername={sublayer_name}",
                    f"{layer_name} ({sublayer_name})",
                    "ogr",
                )
                if candidate.isValid() and candidate.featureCount() > 0:
                    available_layers.append((
                        sublayer_name, candidate, candidate.featureCount()))

            if not available_layers:
                QMessageBox.critical(
                    self, tr("Error"),
                    tr("The GPX file does not contain any importable layers."))
                return

            display_names = [
                f"{name} ({feature_count})"
                for name, _layer, feature_count in available_layers
            ]
            
            from qgis.PyQt.QtWidgets import QInputDialog
            selected_display, ok = QInputDialog.getItem(
                self,
                tr("Import GPX Layer"),
                tr("Choose which layer type to import into QGIS:"),
                display_names,
                0,
                False
            )
            if not ok:
                return
                
            selected_index = display_names.index(selected_display)
            sublayer, import_layer, _feature_count = available_layers[selected_index]
            
            if not import_layer or not import_layer.isValid() or import_layer.featureCount() == 0:
                QMessageBox.critical(
                    self,
                    tr("Error"),
                    tr("Failed to read the GPX layer '{}'. Make sure the file contains this layer type.").format(sublayer)
                )
                return
                
            # Add the selected sublayer to QGIS project!
            QgsProject.instance().addMapLayer(import_layer)
            self.import_button.setVisible(False)
            self.import_button.setEnabled(False)
            QMessageBox.information(
                self,
                tr("Success"),
                tr("Successfully imported layer '{}' into your QGIS project!").format(sublayer)
            )
            # Re-populate combo box to show the newly added layer
            self._populate_layer_combo()
            
        else:
            # Standard temporary layer (no gpx_path), import directly
            if self.layer.id() in QgsProject.instance().mapLayers():
                QMessageBox.information(self, tr("Info"), tr("This layer is already in your QGIS project."))
                self.import_button.setEnabled(False)
                self.import_button.setVisible(False)
                return
            
            QgsProject.instance().addMapLayer(self.layer)
            QMessageBox.information(
                self,
                tr("Success"),
                tr("The GPX layer has been successfully imported into QGIS!")
            )
            self.import_button.setEnabled(False)
            self.import_button.setVisible(False)
            self._populate_layer_combo()


_active_3d_dialog = None


def open_empty_3d_view(iface):
    """Open the native plugin panel without forcing an input selection first."""
    global _active_3d_dialog
    if _active_3d_dialog is not None:
        try:
            _active_3d_dialog.close()
        except (AttributeError, RuntimeError) as error:
            QgsMessageLog.logMessage(
                f"Unable to close the previous 3D viewer: {error}",
                "3D Track Viewer", Qgis.Warning)
    _active_3d_dialog = TrackViewer3DDialog(
        "",
        title=tr("3D Track Viewer"),
        parent=iface.mainWindow(),
        layer=None,
        iface=iface,
    )
    _active_3d_dialog.show()


def open_threejs_3d_view(layer, iface, initial_field=None):
    """
    Extract geometry and field data, generate QGIS-themed 3D HTML,
    and open it embedded inside QGIS using QWebView (with browser fallback option).
    """
    global _active_3d_dialog

    if not layer or not layer.isValid():
        iface.messageBar().pushCritical(tr("3D Track Viewer"), tr("Invalid vector layer!"))
        return

    fields = [f.name() for f in layer.fields()]

    def find_field(keywords):
        for kw in keywords:
            for f in fields:
                f_lower = f.lower()
                if kw in f_lower and not f_lower.endswith(('_s', '_e')):
                    return f
        for kw in keywords:
            for f in fields:
                if kw in f.lower():
                    return f
        return ""

    import math
    from datetime import datetime
    from qgis.core import QgsWkbTypes

    def get_datetime(val):
        if not val:
            return None
        if hasattr(val, "toPyDateTime"):
            try:
                return val.toPyDateTime()
            except (TypeError, ValueError, AttributeError, RuntimeError):
                converted_datetime = None
            if converted_datetime is not None:
                return converted_datetime
        if hasattr(val, "toString"):
            try:
                t_str = val.toString("yyyy-MM-dd HH:mm:ss")
                return datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, AttributeError, RuntimeError):
                parsed_datetime = None
            if parsed_datetime is not None:
                return parsed_datetime
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    return datetime.strptime(val, fmt)
                except ValueError:
                    continue
            try:
                t_str_clean = val.split(".")[0].replace("T", " ").replace("Z", "")
                return datetime.strptime(t_str_clean, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return None

    speed_field = find_field(["speed", "spd"])
    elev_field = find_field(["elev", "ele", "alt"])
    slope_field = find_field(["slope", "slp"])

    def field_has_numeric_values(field_name):
        """A provider may expose optional GPX fields even when every value is NULL."""
        if not field_name:
            return False
        for feature in layer.getFeatures():
            try:
                value = float(feature[field_name])
                if math.isfinite(value):
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
        return False

    def field_has_valid_times(field_name):
        if not field_name:
            return False
        valid_count = 0
        for feature in layer.getFeatures():
            if get_datetime(feature[field_name]) is not None:
                valid_count += 1
                if valid_count >= 2:
                    return True
        return False

    # Empty optional GPX attributes must not suppress derived telemetry.
    if speed_field and not field_has_numeric_values(speed_field):
        speed_field = ""
    if elev_field and not field_has_numeric_values(elev_field):
        elev_field = ""
    if slope_field and not field_has_numeric_values(slope_field):
        slope_field = ""

    time_field = next((name for name in fields if name.lower() == "time"), "")
    has_z = QgsWkbTypes.hasZ(layer.wkbType())
    if has_z:
        # A Z-capable geometry type is not sufficient: some providers fill
        # missing elevations with NaN. Require at least one usable Z value.
        has_z = any(
            math.isfinite(point.z())
            for feature in layer.getFeatures()
            if feature.geometry() and not feature.geometry().isEmpty()
            for point in feature.geometry().vertices()
        )
    elev_available = bool(elev_field) or has_z
    slope_available = elev_available
    speed_available = bool(speed_field) or field_has_valid_times(time_field)

    available_metrics = {
        "speed": speed_available,
        "elevation": elev_available,
        "slope": slope_available,
    }
    requested_metric = str(initial_field or "").lower()
    if requested_metric not in available_metrics or not available_metrics[requested_metric]:
        requested_metric = next(
            (metric for metric in ("elevation", "speed", "slope")
             if available_metrics[metric]),
            "elevation",
        )

    layer_crs = layer.crs()
    if layer_crs.isGeographic():
        ext = layer.extent()
        center_lon, center_lat = ext.center().x(), ext.center().y()
        zone = int((center_lon + 180.0) / 6.0) + 1
        epsg = 32600 + zone if center_lat >= 0 else 32700 + zone
        target_crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        transform = QgsCoordinateTransform(layer_crs, target_crs, QgsProject.instance())
    else:
        target_crs = layer_crs
        transform = None

    def get_attr(f, name):
        if not name or name not in fields:
            return 0.0
        val = f[name]
        try:
            return float(val) if val is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    def format_time(value):
        """Normalize QVariant/QDateTime values before passing them to JavaScript."""
        if value is None:
            return ""
        if isinstance(value, QDateTime):
            return value.toString("yyyy-MM-dd HH:mm:ss") if value.isValid() else ""
        to_datetime = getattr(value, "toPyDateTime", None)
        if callable(to_datetime):
            try:
                return to_datetime().strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, AttributeError):
                converted_text = None
            if converted_text is not None:
                return converted_text
        if hasattr(value, "strftime"):
            try:
                return value.strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                formatted_text = None
            if formatted_text is not None:
                return formatted_text
        return str(value)

    features = []
    for feature_index, feature in enumerate(layer.getFeatures()):
        if feature_index >= MAX_TRACK_FEATURES:
            iface.messageBar().pushWarning(
                tr("3D Track Viewer"),
                tr("The layer exceeds the 100,000-feature safety limit."))
            return
        features.append(feature)
    if not features:
        iface.messageBar().pushWarning(tr("3D Track Viewer"), tr("The layer has no features!"))
        return

    segments = []
    x_vals, y_vals, z_vals = [], [], []
    geom_type = layer.geometryType()

    if geom_type == Qgis.GeometryType.Line:
        for f in features:
            geom = f.geometry()
            if not geom or geom.isEmpty():
                continue

            spd = get_attr(f, speed_field)
            ele = get_attr(f, elev_field)
            slp = get_attr(f, slope_field)
            time_str = format_time(f["time"]) if "time" in fields else ""
            spd_start = get_attr(f, speed_field + "_S") if speed_field + "_S" in fields else spd
            spd_end = get_attr(f, speed_field + "_E") if speed_field + "_E" in fields else spd
            ele_start = get_attr(f, elev_field + "_S") if elev_field + "_S" in fields else ele
            ele_end = get_attr(f, elev_field + "_E") if elev_field + "_E" in fields else ele
            slp_start = get_attr(f, slope_field + "_S") if slope_field + "_S" in fields else slp
            slp_end = get_attr(f, slope_field + "_E") if slope_field + "_E" in fields else slp
            time_end = format_time(f["time_E"]) if "time_E" in fields else time_str

            pts = list(geom.vertices())
            for i in range(len(pts) - 1):
                p1, p2 = pts[i], pts[i+1]
                x1, y1 = p1.x(), p1.y()
                x2, y2 = p2.x(), p2.y()
                z1 = p1.z() if p1.is3D() else ele
                z2 = p2.z() if p2.is3D() else ele

                if transform:
                    pt1_t = transform.transform(QgsPointXY(x1, y1))
                    pt2_t = transform.transform(QgsPointXY(x2, y2))
                    x1, y1 = pt1_t.x(), pt1_t.y()
                    x2, y2 = pt2_t.x(), pt2_t.y()

                x_vals.extend([x1, x2])
                y_vals.extend([y1, y2])
                z_vals.extend([z1, z2])

                # Calculate distance
                d = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)

                # Slope calculation
                if slope_field:
                    final_slope_start = slp_start
                    final_slope_end = slp_end
                else:
                    calc_slope = (z2 - z1) / d * 100 if d > 0.1 else 0.0
                    final_slope_start = calc_slope
                    final_slope_end = calc_slope

                # Elevation calculation
                final_ele_start = ele_start if elev_field else z1
                final_ele_end = ele_end if elev_field else z2

                if len(segments) >= MAX_TRACK_SEGMENTS:
                    iface.messageBar().pushWarning(
                        tr("3D Track Viewer"),
                        tr("The track exceeds the 100,000-segment safety limit."))
                    return
                segments.append({
                    "x1": x1, "y1": y1, "z1": z1,
                    "x2": x2, "y2": y2, "z2": z2,
                    "speed": spd_start, "speed2": spd_end,
                    "elevation": final_ele_start, "elevation2": final_ele_end,
                    "slope": final_slope_start, "slope2": final_slope_end,
                    "time": time_str, "time2": time_end
                })
    else:
        prev_attr = None
        for f in features:
            geom = f.geometry()
            if not geom or geom.isEmpty():
                continue

            pts = list(geom.vertices())
            if not pts:
                continue

            curr_pt = pts[0]
            spd = get_attr(f, speed_field)
            ele = get_attr(f, elev_field)
            slp = get_attr(f, slope_field)
            time_val = f["time"] if "time" in fields else None
            time_str = format_time(time_val)

            curr_z = curr_pt.z() if curr_pt.is3D() else ele

            curr_attr = {
                "pt": curr_pt,
                "z": curr_z,
                "speed": spd,
                "elevation": ele,
                "slope": slp,
                "time": time_str,
                "time_val": time_val
            }

            if prev_attr:
                p1, p2 = prev_attr["pt"], curr_attr["pt"]
                x1, y1 = p1.x(), p1.y()
                x2, y2 = p2.x(), p2.y()
                z1, z2 = prev_attr["z"], curr_attr["z"]

                if transform:
                    pt1_t = transform.transform(QgsPointXY(x1, y1))
                    pt2_t = transform.transform(QgsPointXY(x2, y2))
                    x1, y1 = pt1_t.x(), pt1_t.y()
                    x2, y2 = pt2_t.x(), pt2_t.y()

                x_vals.extend([x1, x2])
                y_vals.extend([y1, y2])
                z_vals.extend([z1, z2])

                # Calculate distance
                d = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)

                # Slope calculation
                if slope_field:
                    final_slope_start = prev_attr["slope"]
                    final_slope_end = curr_attr["slope"]
                else:
                    calc_slope = (z2 - z1) / d * 100 if d > 0.1 else 0.0
                    final_slope_start = calc_slope
                    final_slope_end = calc_slope

                # Elevation calculation
                final_ele_start = prev_attr["elevation"] if elev_field else z1
                final_ele_end = curr_attr["elevation"] if elev_field else z2

                # Speed calculation
                if speed_field:
                    final_speed_start = prev_attr["speed"]
                    final_speed_end = curr_attr["speed"]
                else:
                    final_speed_start = 0.0
                    final_speed_end = 0.0
                    t1 = get_datetime(prev_attr["time_val"])
                    t2 = get_datetime(curr_attr["time_val"])
                    if t1 and t2:
                        dt = (t2 - t1).total_seconds()
                        if dt > 0.0:
                            calc_spd = (d / dt) * 3.6
                            final_speed_start = calc_spd
                            final_speed_end = calc_spd

                if len(segments) >= MAX_TRACK_SEGMENTS:
                    iface.messageBar().pushWarning(
                        tr("3D Track Viewer"),
                        tr("The track exceeds the 100,000-segment safety limit."))
                    return
                segments.append({
                    "x1": x1, "y1": y1, "z1": z1,
                    "x2": x2, "y2": y2, "z2": z2,
                    "speed": final_speed_start, "speed2": final_speed_end,
                    "elevation": final_ele_start, "elevation2": final_ele_end,
                    "slope": final_slope_start, "slope2": final_slope_end,
                    "time": prev_attr["time"], "time2": curr_attr["time"]
                })

            prev_attr = curr_attr

    if not segments:
        iface.messageBar().pushWarning(tr("3D Track Viewer"), tr("No valid 3D geometry segments found in layer!"))
        return

    # Ground-up Spatial-Temporal Telemetry Pipeline (Gaussian Kernel Smoothing for Slope & Speed)
    n_segs = len(segments)
    if n_segs > 0:
        window_dist = 15.0
        sigma = max(5.0, window_dist / 2.0)
        
        # 1. Compute cumulative 2D distances for segment nodes
        cum_dists = [0.0] * (n_segs + 1)
        for i in range(n_segs):
            s = segments[i]
            d_seg = math.sqrt((s["x2"] - s["x1"])**2 + (s["y2"] - s["y1"])**2)
            cum_dists[i+1] = cum_dists[i] + d_seg
            
        # 2. Gaussian Spatial Kernel Elevation Smoothing (for slope derivation)
        if not slope_field:
            node_z = [segments[i]["z1"] for i in range(n_segs)] + [segments[-1]["z2"]]
            smoothed_z = [0.0] * (n_segs + 1)
            for i in range(n_segs + 1):
                w_sum = 0.0
                z_sum = 0.0
                lower_distance = cum_dists[i] - window_dist * 2.0
                upper_distance = cum_dists[i] + window_dist * 2.0
                lower_index = bisect_left(
                    cum_dists, lower_distance,
                    max(0, i - MAX_SMOOTHING_NEIGHBORS), i + 1)
                upper_index = bisect_right(
                    cum_dists, upper_distance, i,
                    min(n_segs + 1, i + MAX_SMOOTHING_NEIGHBORS + 1))
                for j in range(lower_index, upper_index):
                    dist_ij = abs(cum_dists[j] - cum_dists[i])
                    w = math.exp(-0.5 * (dist_ij / sigma)**2)
                    w_sum += w
                    z_sum += w * node_z[j]
                smoothed_z[i] = z_sum / w_sum if w_sum > 0 else node_z[i]

        # 3. Compute continuous Spatial-Temporal Speed & Slope vectors
        for i in range(n_segs):
            left = i
            while left > 0 and (cum_dists[i] - cum_dists[left]) < window_dist:
                left -= 1
            right = i + 1
            while right < n_segs and (cum_dists[right] - cum_dists[i+1]) < window_dist:
                right += 1

            w_dist = cum_dists[right] - cum_dists[left]

            # Slope calculation
            if not slope_field:
                w_dz = smoothed_z[right] - smoothed_z[left]
                slope = (w_dz / w_dist) * 100.0 if w_dist > 0.5 else 0.0
                segments[i]["slope"] = slope
                segments[i]["slope2"] = slope
                segments[i]["elevation"] = smoothed_z[i]
                segments[i]["elevation2"] = smoothed_z[i+1]

            # Speed calculation
            if not speed_field:
                right_seg = min(n_segs - 1, right)
                t_left = get_datetime(segments[left]["time"])
                t_right = get_datetime(segments[right_seg].get("time2") or segments[right_seg]["time"])
                if t_left and t_right:
                    dt_win = (t_right - t_left).total_seconds()
                    if dt_win > 0:
                        spd = (w_dist / dt_win) * 3.6  # m/s -> km/h
                        segments[i]["speed"] = spd
                        segments[i]["speed2"] = spd

    cx = (min(x_vals) + max(x_vals)) / 2.0
    cy = (min(y_vals) + max(y_vals)) / 2.0
    cz_min = min(z_vals)
    cz_avg = sum(z_vals) / len(z_vals)
    dx = max(x_vals) - min(x_vals)
    dy = max(y_vals) - min(y_vals)
    diag = max(500.0, (dx*dx + dy*dy)**0.5)

    for s in segments:
        s["x1"] -= cx
        s["y1"] -= cy
        s["x2"] -= cx
        s["y2"] -= cy

    _, speed_unit = detect_field_and_unit(layer, "speed")
    _, elev_unit = detect_field_and_unit(layer, "elevation")
    _, slope_unit = detect_field_and_unit(layer, "slope")
    # Derived speed values above are already in km/h. Labeling them as m/s
    # makes the web viewer convert the colour-range values a second time.
    speed_unit = speed_unit or ("m/s" if speed_field else "km/h")
    elev_unit = elev_unit or "m"
    # Derived slope is rise/run * 100, so its unit is percent, not degrees.
    slope_unit = slope_unit or ("°" if slope_field else "%")

    track_data = {
        "title": layer.name(),
        "segments": segments,
        "z_base": cz_min,
        "z_avg": cz_avg,
        "extent_diag": diag,
        "units": {
            "speed": speed_unit,
            "elevation": elev_unit,
            "slope": slope_unit
        },
        "available_metrics": available_metrics
    }

    theme = qgis_theme_colors(iface.mainWindow())
    html_content = generate_threejs_html(
        track_data,
        title=f"3D Track Viewer - {layer.name()}",
        theme=theme,
        initial_field=requested_metric,
    )
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", suffix=".html",
            prefix="3d_track_viewer_", delete=False) as temporary_html:
        temporary_html.write(html_content)
        html_path = temporary_html.name

    _active_3d_dialog = TrackViewer3DDialog(
        html_path,
        title=layer.name(),
        parent=iface.mainWindow(),
        layer=layer,
        iface=iface,
    )
    _active_3d_dialog.show()

    if not HAS_WEBVIEW:
        QDesktopServices.openUrl(QUrl.fromLocalFile(html_path))
