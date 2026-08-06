(function () {
    "use strict";

    var state = { fileName: "尚未载入轨迹", toastTimer: null };

    function byId(id) { return document.getElementById(id); }

    function setText(selector, text) {
        var element = document.querySelector(selector);
        if (element) element.textContent = text;
    }

    function setButton(id, text, title) {
        var button = byId(id);
        if (!button) return;
        button.textContent = text;
        button.title = title || text;
        button.setAttribute("aria-label", title || text);
    }

    function showToast(message, type) {
        var toast = byId("app-toast");
        if (!toast) return;
        toast.textContent = message;
        toast.className = "toast visible" + (type === "error" ? " error" : "");
        window.clearTimeout(state.toastTimer);
        state.toastTimer = window.setTimeout(function () {
            toast.className = "toast";
        }, 3200);
    }

    function createTopbar() {
        var header = document.createElement("header");
        header.className = "app-topbar";
        header.innerHTML = [
            '<div class="brand-lockup">',
            '  <div class="brand-mark" aria-hidden="true">3D</div>',
            '  <div class="brand-copy"><strong class="brand-title">Track Viewer</strong><span class="brand-file" id="active-file-name">尚未载入轨迹</span></div>',
            '</div>',
            '<div class="topbar-status" id="app-status">GPX 文件仅在当前浏览器中处理</div>',
            '<div class="topbar-actions">',
            '  <button type="button" class="topbar-btn" id="new-track-button">打开 GPX</button>',
            '  <button type="button" class="topbar-btn primary" id="drawer-toggle-button" aria-expanded="false">轨迹样式</button>',
            '</div>'
        ].join("");
        document.body.insertBefore(header, document.body.firstChild);

        byId("new-track-button").addEventListener("click", openFileDialog);
        byId("drawer-toggle-button").addEventListener("click", toggleDrawer);
    }

    function createToast() {
        var toast = document.createElement("div");
        toast.id = "app-toast";
        toast.className = "toast";
        toast.setAttribute("role", "status");
        toast.setAttribute("aria-live", "polite");
        document.body.appendChild(toast);
    }

    function openFileDialog() {
        var input = byId("gpx-file-input");
        if (input) input.click();
    }

    function toggleDrawer() {
        var sidebar = document.querySelector(".sidebar");
        var button = byId("drawer-toggle-button");
        if (!sidebar || !button) return;
        var willOpen = sidebar.classList.contains("collapsed");
        sidebar.classList.toggle("collapsed", !willOpen);
        button.setAttribute("aria-expanded", willOpen ? "true" : "false");
        button.textContent = willOpen ? "关闭设置" : "轨迹样式";
    }

    function modernizeUpload() {
        var box = byId("upload-box");
        if (!box) return;
        box.innerHTML = [
            '<div class="upload-copy">',
            '  <div class="upload-kicker">浏览器端 3D 轨迹工具</div>',
            '  <h2>把一段轨迹<br>变成空间体验</h2>',
            '  <p>打开 GPX 文件，查看速度、高程与坡度变化。旋转视角、裁剪轨迹并导出清晰图像。</p>',
            '  <input type="file" id="gpx-file-input" accept=".gpx,application/gpx+xml" hidden>',
            '  <button type="button" class="btn" id="hero-file-button">选择 GPX 文件</button>',
            '</div>',
            '<div class="drop-visual" id="drop-visual">',
            '  <div class="drop-contours">',
            '    <img src="assets/terrain-track-hero.png" alt="深色三维地形上的登山轨迹">',
            '    <div class="drop-hint">也可以把 GPX 文件拖到这里</div>',
            '    <div class="privacy-note">轨迹不会上传到服务器</div>',
            '  </div>',
            '</div>'
        ].join("");

        var input = byId("gpx-file-input");
        var visual = byId("drop-visual");
        byId("hero-file-button").addEventListener("click", openFileDialog);
        input.addEventListener("change", function (event) {
            if (event.target.files && event.target.files[0]) {
                handleSelectedFile(event.target.files[0]);
            }
        });
        ["dragenter", "dragover"].forEach(function (name) {
            visual.addEventListener(name, function (event) {
                event.preventDefault();
                box.classList.add("hover");
            });
        });
        ["dragleave", "drop"].forEach(function (name) {
            visual.addEventListener(name, function (event) {
                event.preventDefault();
                box.classList.remove("hover");
            });
        });
        visual.addEventListener("drop", function (event) {
            var file = event.dataTransfer.files && event.dataTransfer.files[0];
            if (file) handleSelectedFile(file);
        });
    }

    function handleSelectedFile(file) {
        if (!/\.gpx$/i.test(file.name)) {
            showToast("请选择扩展名为 .gpx 的轨迹文件", "error");
            return;
        }
        state.fileName = file.name;
        if (window.trackData) window.trackData.title = file.name;
        setText("#active-file-name", file.name);
        setText("#app-status", "正在解析轨迹");
        if (typeof window.loadGPXFile === "function") window.loadGPXFile(file);
        window.setTimeout(function () {
            var overlay = byId("upload-overlay");
            if (overlay && overlay.style.display === "none") {
                setText("#app-status", "轨迹已就绪");
                showToast("GPX 轨迹已载入");
            }
        }, 220);
    }

    function modernizeSidebar() {
        var sidebar = document.querySelector(".sidebar");
        if (!sidebar) return;
        sidebar.classList.add("collapsed");
        setText(".panel-title", "轨迹样式");

        var groups = sidebar.querySelectorAll(".control-group");
        if (groups[0]) setText(".control-group:nth-of-type(1) > label", "着色指标");
        if (groups[1]) {
            var span = groups[1].querySelector("label > span:first-child");
            if (span) span.textContent = "垂直夸张";
            var input = byId("exag-input");
            if (input) input.setAttribute("aria-label", "垂直夸张倍数");
        }
        if (groups[2]) {
            var widthLabel = groups[2].querySelector("label > span:first-child");
            if (widthLabel) widthLabel.textContent = "轨迹粗细";
            var widthInput = byId("width-input");
            if (widthInput) widthInput.setAttribute("aria-label", "轨迹粗细");
        }

        var select = byId("field-select");
        if (select) {
            select.options[0].textContent = "速度";
            select.options[1].textContent = "高程";
            select.options[2].textContent = "坡度";
        }
        setButton("legend-toggle", "显示图例", "显示或隐藏视口图例");
        setButton("axis-toggle", "显示坐标轴", "显示或隐藏方向坐标轴");
        setText(".legend-title label", "颜色范围");
        var rangeMode = byId("color-range-mode");
        if (rangeMode) {
            rangeMode.options[0].textContent = "自动";
            rangeMode.options[1].textContent = "按裁剪范围";
            rangeMode.options[2].textContent = "手动";
        }
        setButton("gradient-toggle", "线段渐变", "开启或关闭线段渐变");
        setButton("settings-btn", "单位设置", "设置数据显示单位");
    }

    function modernizeToolbar() {
        var icons = [
            "open_browser.svg",
            "Highly_adaptive.svg", "Scale_to_trajectory.svg", "Reset_Perspective.svg",
            "Trajectory_clipping.svg", "key_frame.svg", "follow_display.svg", "Setting.svg",
            "export_image.svg", "export_svg.svg"
        ];
        var labels = [
            ["打开轨迹", "打开 GPX 轨迹文件"],
            ["自适应高度", "自动计算合适的垂直比例"],
            ["缩放至轨迹", "将当前可见轨迹适配到视口"],
            ["重置视角", "恢复默认三维视角"],
            ["时间裁剪", "按时间范围裁剪轨迹"],
            ["关键帧动画", "编辑相机关键帧动画"],
            ["跟随显示", "播放时让相机跟随轨迹"],
            ["设置", "轨迹分析单位设置"],
            ["导出 PNG", "将当前视图导出为图片"],
            ["导出 SVG", "将当前视图导出为SVG"]
        ];
        var buttons = document.querySelectorAll("#web-toolbar .tool-btn");
        Array.prototype.forEach.call(buttons, function (button, index) {
            if (!labels[index]) return;
            button.innerHTML = '<img src="assets/icons/' + icons[index] + '" alt="">';
            button.title = labels[index][1];
            button.setAttribute("aria-label", labels[index][1]);
        });
    }

    function modernizeTimeAndModal() {
        var timeControl = byId("time-range-control");
        if (timeControl) timeControl.setAttribute("aria-label", "轨迹时间范围");
        var startSlider = byId("time-start-slider");
        var endSlider = byId("time-end-slider");
        var playhead = byId("time-playhead-slider");
        if (startSlider) startSlider.setAttribute("aria-label", "片段开始时间");
        if (endSlider) endSlider.setAttribute("aria-label", "片段结束时间");
        if (playhead) playhead.setAttribute("aria-label", "动画播放位置");

        setText("#settings-modal .modal-header h3", "单位设置");
        setText('label[for="settings-unit-elevation"]', "高程单位");
        setText('label[for="settings-unit-speed"]', "速度单位");
        setText('label[for="settings-unit-slope"]', "坡度单位");
        var close = document.querySelector("#settings-modal .modal-close");
        if (close) { close.textContent = "关闭"; close.setAttribute("aria-label", "关闭单位设置"); }
        var save = document.querySelector("#settings-modal .btn-save");
        if (save) save.textContent = "保存设置";
        var cancel = document.querySelector("#settings-modal .modal-footer .btn:not(.btn-save)");
        if (cancel) cancel.textContent = "取消";
    }

    function modernizeLoading() {
        setText(".loading-text", "正在构建三维轨迹，请稍候");
    }

    function observeViewerState() {
        var upload = byId("upload-overlay");
        if (!upload || !window.MutationObserver) return;
        var observer = new MutationObserver(function () {
            if (upload.style.display === "none") {
                setText("#app-status", "轨迹已就绪");
                var drawer = byId("drawer-toggle-button");
                if (drawer) drawer.disabled = false;
            }
        });
        observer.observe(upload, { attributes: true, attributeFilter: ["style"] });
    }

    function installErrorPresentation() {
        window.alert = function (message) {
            var clean = String(message || "操作未完成");
            if (clean.indexOf("GPX") >= 0) clean = "无法解析该 GPX 文件，请确认文件包含有效轨迹点";
            showToast(clean, "error");
        };
    }

    function init() {
        document.documentElement.lang = "zh-CN";
        document.title = "3D Track Viewer | 在线 GPX 轨迹查看器";
        if (window.theme) {
            window.theme.window = "#0d1110";
            window.theme.base = "#151a18";
            window.theme.text = "#edf2ef";
            window.theme.muted = "#9aa69f";
            window.theme.border = "#344039";
            window.theme.button = "#191f1c";
            window.theme.highlight = "#78b892";
            window.theme.highlight_text = "#09110c";
            window.theme.grid = "#52605a";
            if (typeof window.applyTheme === "function") window.applyTheme(window.theme);
        }
        window.groundBackground = "#111714";
        window.skyBackground = "#18221d";
        createTopbar();
        createToast();
        modernizeUpload();
        modernizeSidebar();
        modernizeToolbar();
        modernizeTimeAndModal();
        modernizeLoading();
        observeViewerState();
        installErrorPresentation();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }
}());
