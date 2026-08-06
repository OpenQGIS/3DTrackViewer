# -*- coding: utf-8 -*-
"""
3D Track Viewer Plugin Controller
=================================
Manages menu items, toolbars, and layer loading for QGIS.
"""

import os
import re
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTranslator, QLocale
from qgis.PyQt.QtGui import QIcon
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt 5
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                 QLabel, QComboBox, QPushButton, QFileDialog,
                                 QMessageBox)
from qgis.core import QgsMapLayer, QgsProject, QgsVectorLayer, Qgis, QgsSettings

from .viewer_3d import open_empty_3d_view


MAX_GPX_FILE_BYTES = 100 * 1024 * 1024

_zh_translations = {
    "3D Track": "3D 轨迹",
    "3D Track Viewer": "3D 轨迹查看器",
    "Open 3D Track Viewer": "打开 3D 轨迹查看器",
    "Select Track Input": "选择轨迹输入",
    "Select Layer in Current Project:": "选择当前工程中的图层：",
    "No compatible point/line layers found in project.": "工程中未找到兼容的点/线图层。",
    "📂 Open Local GPX File...": "📂 打开本地 GPX 文件...",
    "Open 3D View": "打开 3D 视图",
    "Cancel": "取消",
    "Select GPX File": "选择 GPX 文件",
    "Error": "错误",
    "Unable to inspect the selected GPX file: {}": "无法检查选定的 GPX 文件：{}",
    "GPX file is too large": "GPX 文件过大",
    "The selected GPX file exceeds the 100 MB safety limit.": "所选 GPX 文件超过 100 MB 安全限制。",
    "Failed to read the GPX file as track geometry.": "无法将 GPX 文件读取为轨迹几何图形。",
    "Import GPX Layer": "导入 GPX 图层",
    "Choose which layer type to import into QGIS:": "选择要导入至 QGIS 的图层类型：",
    "The GPX layer has been successfully imported into QGIS!": "GPX 图层已成功导入至 QGIS！",
    "This layer is already in your QGIS project.": "该图层已在您的 QGIS 工程中。",
    "Import this temporary GPX layer into your QGIS project layers list": "将此临时 GPX 图层导入到您的 QGIS 工程图层列表中",
    "Switch currently displayed track layer": "切换当前显示轨迹图层",
    "Open Full 3D View in System Browser": "在系统浏览器中打开完整 3D 视图",
    "Embedded 3D is not supported in current QGIS environment. Opened in system browser.": "当前 QGIS 环境不支持嵌入式 3D 视图，已在系统浏览器中打开。",
    "Height Auto-fit": "高度自适应",
    "Zoom to Layer": "缩放到图层",
    "Reset View": "重置视图",
    "Track Time Clipping": "轨迹时间裁剪",
    "Track Time Clipping: Current layer does not have sufficient time information": "轨迹时间裁剪：当前图层缺乏足够的时间信息",
    "Keyframe Animation": "关键帧动画",
    "Follow Display": "跟随显示",
    "Follow Display: Show time, elevation, speed, and slope along track": "跟随显示：沿轨迹显示时间、高程、速度与坡度",
    "Export Image": "将当前视图导出为图片",
    "Image export failed.": "图片导出失败。",
    "Export SVG": "将当前视图导出为SVG",
    "SVG export failed:": "SVG 导出失败：",
    "SVG export failed: {}": "SVG 导出失败：{}",
    "Export PDF": "将当前视图导出为PDF",
    "Vector scene unavailable. Please wait for 3D view to load and try again.": "矢量场景不可用，请等待 3D 视图加载完成后重试。",
    "Invalid vector scene, PDF export canceled.": "无效的矢量场景，PDF 导出已取消。",
    "Unable to create PDF painter device.": "无法创建 PDF 绘图设备。",
    "Export GIF": "导出为GIF动画",
    "Export Keyframe Animation as GIF": "导出关键帧动画为 GIF",
    "Frames per second (5-25):": "每秒帧数 (5-25)：",
    "Animation speed:": "动画倍数：",
    "Frames per second:": "每秒帧数：",
    "Animation duration:": "动画时长：",
    "Total frames:": "总帧数：",
    "{:.1f} seconds": "{:.1f} 秒",
    "{} frames": "{} 帧",
    "Export": "导出",
    "GIF export requires the Pillow Python package in the QGIS environment.": "GIF 导出需要 QGIS 环境中安装 Pillow Python 包。",
    "Unable to read keyframe animation settings.": "无法读取关键帧动画设置。",
    "Add at least two valid keyframes inside the clipping interval before exporting GIF.": "导出 GIF 之前，请在裁剪区间内添加至少两个有效关键帧。",
    "GIF export exceeds the 450-frame safety limit. Increase playback speed or reduce FPS.": "GIF 导出超过 450 帧安全限制。请提高播放速度或降低 FPS。",
    "GIF export would exceed the 512 MB memory safety limit. Reduce the window size, playback duration, or FPS.": "GIF 导出将超过 512 MB 内存安全限制。请减小窗口尺寸、播放时长或 FPS。",
    "Animation export could not be initialized.": "无法初始化动画导出。",
    "Rendering GIF frames...": "正在渲染 GIF 帧...",
    "Unable to capture an animation frame.": "无法捕获动画帧。",
    "No animation frames were generated.": "未生成任何动画帧。",
    "GIF export failed:": "GIF 导出失败：",
    "GIF export failed: {}": "GIF 导出失败：{}",
    "Export 3D Image": "3D视图导出为图片",
    "PNG Images (*.png)": "PNG 图片 (*.png)",
    "JPEG Images (*.jpg *.jpeg)": "JPEG 图片 (*.jpg *.jpeg)",
    "BMP Images (*.bmp)": "BMP 图片 (*.bmp)",
    "Image format:": "图片格式：",
    "Width:": "宽度：",
    "Height:": "高度：",
    "Keep aspect ratio": "保持宽高比",
    "Export 3D GIF": "导出为GIF动画",
    "GIF Animations (*.gif)": "GIF 动画 (*.gif)",
    "Export 3D SVG": "导出 3D SVG",
    "SVG Vector Graphics (*.svg)": "SVG 矢量图 (*.svg)",
    "Export 3D PDF": "导出 3D PDF",
    "PDF Documents (*.pdf)": "PDF 文档 (*.pdf)",
    "Invalid vector layer!": "无效的矢量图层！",
    "The layer exceeds the 100,000-feature safety limit.": "图层超过 100,000 个要素安全限制。",
    "The layer has no features!": "图层中没有任何要素！",
    "The track exceeds the 100,000-segment safety limit.": "轨迹超过 100,000 个线段安全限制。",
    "No valid 3D geometry segments found in layer!": "图层中未找到有效的 3D 几何线段！",
    "Failed to read the GPX layer '{}'. Make sure the file contains this layer type.": "无法读取 GPX 图层 '{}'。请确保文件包含此图层类型。",
    "Successfully imported layer '{}' into your QGIS project!": "已成功将图层 '{}' 导入至您的 QGIS 工程！",
    "Success": "成功",
    "Info": "信息",
    "Open in Browser": "在浏览器中打开",
    "📥 导入至QGIS": "📥 导入至 QGIS",
    "Import to QGIS": "导入至 QGIS",
    "Open GPX File": "打开 GPX 文件",
    "Open another local GPX file": "打开其他本地 GPX 文件",
    "3D Track Viewer Settings": "轨迹分析单位设置",
    "Hover display and units": "悬停显示与单位",
    "Unit and hover settings": "单位与悬停设置",
    "Time": "时间",
    "Elevation": "高程",
    "Speed": "速度",
    "Slope": "坡度",
    "Settings": "设置",
    "OK": "确定",
    "GPX Files (*.gpx)": "GPX 文件 (*.gpx)",
    "Please select an existing GPX file.": "请选择一个存在的 GPX 文件。",
    "The GPX file does not contain any importable layers.": "该 GPX 文件不包含任何可导入的图层。",
    "Select QGIS layer...": "选择 QGIS 图层...",
    "Select a QGIS layer or open a local GPX file from the upper-left toolbar.": "请从左上角工具栏选择 QGIS 图层或打开本地 GPX 文件。",
}


def tr(text, disambiguation=None):
    res = QCoreApplication.translate('3DTrackViewer', text, disambiguation)
    if res == text:
        locale_name = QgsSettings().value("locale/userLocale", QLocale.system().name())
        if locale_name and str(locale_name).lower().startswith("zh"):
            if text in _zh_translations:
                return _zh_translations[text]
    return res



class TrackViewerLayerDialog(QDialog):
    """
    Dialog allowing the user to select an existing layer in QGIS,
    or browse for a local GPX file.
    """
    def __init__(self, iface, active_layer=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle(tr("Select Track Input"))
        self.setMinimumWidth(380)
        self._selected_layer = None
        self._gpx_path = None
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel(tr("Select Layer in Current Project:")))
        self.combo = QComboBox()
        layout.addWidget(self.combo)
        
        # Populate Point and Line layers from project
        self._layers = []
        active_idx = -1
        for layer in QgsProject.instance().mapLayers().values():
            if layer and layer.isValid() and layer.type() == QgsMapLayer.LayerType.VectorLayer:
                if layer.geometryType() in (Qgis.GeometryType.Point, Qgis.GeometryType.Line):
                    self._layers.append(layer)
                    self.combo.addItem(layer.name(), layer)
                    if active_layer and layer.id() == active_layer.id():
                        active_idx = self.combo.count() - 1
                        
        if active_idx >= 0:
            self.combo.setCurrentIndex(active_idx)
            
        if not self._layers:
            self.combo.setEnabled(False)
            tip = QLabel(tr("No compatible point/line layers found in project."))
            tip.setStyleSheet("color: #888; font-style: italic;")
            layout.addWidget(tip)
            
        layout.addSpacing(15)
        
        btn_gpx = QPushButton(tr("📂 Open Local GPX File..."))
        btn_gpx.setMinimumHeight(32)
        btn_gpx.clicked.connect(self._on_browse_gpx)
        layout.addWidget(btn_gpx)
        
        layout.addSpacing(15)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.btn_ok = QPushButton(tr("Open 3D View"))
        self.btn_ok.setEnabled(bool(self._layers))
        self.btn_ok.clicked.connect(self._on_accept)
        # Style the primary button
        self.btn_ok.setStyleSheet(
            "font-weight: bold; background-color: #0066cc; color: white; padding: 4px 12px;"
        )
        
        btn_cancel = QPushButton(tr("Cancel"))
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.btn_ok)
        btn_layout.addWidget(btn_cancel)
        
        layout.addLayout(btn_layout)
        
    def _on_browse_gpx(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Select GPX File"), "", "GPX Files (*.gpx)"
        )
        if path:
            self._gpx_path = path
            self.accept()
            
    def _on_accept(self):
        idx = self.combo.currentIndex()
        if idx >= 0:
            self._selected_layer = self.combo.itemData(idx)
        self.accept()
        
    def selected_layer(self):
        return self._selected_layer
        
    def gpx_path(self):
        return self._gpx_path


class ThreeDTrackViewer:
    """
    Main plugin class registered in QGIS.
    """
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.toolbar = None

        # Fetch locale and sanitize to prevent path traversal
        locale_name = QgsSettings().value("locale/userLocale", QLocale.system().name())
        if locale_name is not None:
            locale_name = str(locale_name)
        else:
            locale_name = "en"
        if not re.match(r"^[a-zA-Z0-9_\-]+$", locale_name):
            locale_name = "en"

        _locale_map = {
            "zh-hans": "zh_CN",
            "zh-hant": "zh_TW",
            "zh-cn":   "zh_CN",
            "zh-tw":   "zh_TW",
        }
        locale_candidates = []
        _key = locale_name.lower()
        if _key in _locale_map:
            locale_candidates.append(_locale_map[_key])
        locale_candidates.append(locale_name.replace("-", "_"))
        locale_candidates.append(locale_name.split("_")[0].split("-")[0])

        self.translator = QTranslator()
        i18n_dir = os.path.join(self.plugin_dir, "i18n")
        for candidate in locale_candidates:
            if self.translator.load(f"3DTrackViewer_{candidate}", i18n_dir):
                QCoreApplication.installTranslator(self.translator)
                break

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icons", "icon.svg")
        self.action = QAction(QIcon(icon_path), tr("Open 3D Track Viewer"), self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        
        self.iface.addPluginToVectorMenu(tr("3D Track Viewer"), self.action)
        self.toolbar = self.iface.addToolBar(tr("3D Track Viewer"))
        self.toolbar.setObjectName("3DTrackViewer")
        self.toolbar.addAction(self.action)
        
    def unload(self):
        if self.action:
            self.iface.removePluginVectorMenu(tr("3D Track Viewer"), self.action)
            if self.toolbar:
                self.iface.mainWindow().removeToolBar(self.toolbar)
        if hasattr(self, 'translator') and self.translator:
            QCoreApplication.removeTranslator(self.translator)
                
    def run(self):
        # Input selection belongs to the viewer's native toolbar.  Opening the
        # plugin action should never block on a separate selection dialog.
        open_empty_3d_view(self.iface)
