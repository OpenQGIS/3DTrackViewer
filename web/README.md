# 3D Track Viewer Website

此目录包含可独立发布的 3D Track Viewer 网站，与 QGIS 插件运行文件相互独立。

## 目录内容

- `index.html`：网站入口。
- `web.css`：网站布局、主题和响应式样式。
- `web-app.js`：文件载入、界面状态及网站交互逻辑。
- `assets/`：网站使用的图标和视觉资源。

## 功能

- 在用户浏览器中打开并解析本地 GPX 文件。
- 三维显示轨迹的高程、速度和坡度变化。
- 调整轨迹外观、色阶和垂直比例。
- 使用时间裁剪、跟随显示及关键帧动画。
- 导出 PNG 和 SVG。
- 支持桌面端和移动端布局。

GPX 文件默认只在当前浏览器中处理，不会上传到服务器。

## 本地预览

可以直接打开 `index.html`。如浏览器限制本地资源加载，建议在 `web` 目录启动静态文件服务器：

```bash
python -m http.server 8000
```

然后访问 `http://localhost:8000/`。

## GitHub Pages 发布

将 `web` 目录作为静态站点发布源，或者通过 GitHub Actions 将该目录上传为 Pages 构建产物。网站不需要服务端程序或数据库。

## 本地依赖

网站使用的固定版本 Three.js 和 OrbitControls 保存在 `vendor/` 中，运行时不从公共 CDN 加载脚本。
