# DOWN — 多平台无水印下载器

> 三个 UI 版本共享一个后端，一键切换

## 在线地址

| 域名 | 项目 | 端口 |
|------|------|------|
| [fzpnowm.top](https://fzpnowm.top) | 抖音/X 无水印下载器 | 9000 |


## 版本

| 路由 | 版本 | 风格 |
|------|------|------|
| `/` | 🤍 Liquid Glass 3.0 | Apple 液态玻璃白主题，GSAP 动画，鼠标光迹 |
| `/v1` | 🖤 Classic Dark | 经典暗色主题 |
| `/v2` | 💎 WebGL Liquid Glass | WebGL 实时折射玻璃渲染 |

右上角菜单可随时切换。

## 支持平台

抖音 / 快手 / B站 / TikTok / Twitter(X)

## 快速启动

```bash
pip install -r requirements.txt
# 安装 Playwright 浏览器（动图解析需要）
python -m playwright install chromium
python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

访问 http://127.0.0.1:8001

## 文件结构

```
DOWN/
├── main.py          FastAPI 路由 + 流式代理
├── downloader.py    核心下载引擎
├── requirements.txt fastapi, uvicorn, httpx, yt-dlp
├── static/
│   ├── index.html        主 UI（Liquid Glass 3.0）
│   ├── index-v1.html     经典暗色 UI
│   ├── index-v2.html     WebGL 玻璃 UI
│   ├── liquidGL.js       液态玻璃渲染
│   ├── html2canvas.min.js
│   ├── gsap.min.js
│   ├── logo.jpg
│   └── bg.png / bg2.jpg / bg3.png
├── downloads/       临时下载目录（自动清理）
├── DEPLOY.md        部署文档
└── README.md
```

## 后端架构 (`main.py`)

| 端点 | 方法 | 功能 |
|------|------|------|
| `/` `/v1` `/v2` | GET | 三个 UI 页面 |
| `/api/parse` | POST | 解析链接，返回视频/图片信息 |
| `/api/stream` | GET | 流式代理播放（Range 支持，可拖进度条） |
| `/api/download` | POST | 下载文件（支持进度条） |

## 流式下载

### 原理

传统的下载方式是 `httpx.get` 一次性将整个文件加载到内存，然后返回给浏览器。对于大文件（如 42 分钟的视频，约 370MB），这会导致：

- 内存占用过高
- 下载超时
- 进程卡死

流式下载改为：**边从源站读取，边写入磁盘，边返回给浏览器**。

### 实现

#### 1. 视频下载（`_download_douyin_video`）

```python
with httpx.stream("GET", video_url, headers=headers, follow_redirects=True,
                   timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10)) as r:
    with open(filepath, "wb") as f:
        for chunk in r.iter_bytes(65536):
            f.write(chunk)
```

- `httpx.stream`：流式请求，不一次性读取全部内容
- `iter_bytes(65536)`：每次读取 64KB
- 写入磁盘：边下载边写入，内存占用恒定 64KB
- 超时设置：连接 10s，读取 300s（单次 chunk 超时，非总超时）

#### 2. 流式代理（`/api/stream`）

```python
client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10))
stream_resp = await client.send(req, stream=True)

async def chunk_iter():
    async for chunk in stream_resp.aiter_bytes(65536):
        yield chunk

return StreamingResponse(chunk_iter(), status_code=206, headers=resp_headers)
```

- 支持 HTTP Range 请求（浏览器拖进度条时发送 Range 头）
- 返回 206 Partial Content 状态码
- 边从源站读取边返回给浏览器，实现**边下边播**

#### 3. 下载接口（`/api/download`）

```python
def file_iter():
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            yield chunk

return StreamingResponse(file_iter(), headers={"Content-Length": str(file_size)})
```

- 从磁盘流式读取已下载的文件
- 设置 `Content-Length` 头，浏览器可显示下载进度
- 内存占用恒定 64KB


### 超时配置

| 参数 | 值 | 说明 |
|------|-----|------|
| connect | 10s | 建立连接超时 |
| read | 300s | 读取单个 chunk 超时（非总时间） |
| write | 10s | 写入超时 |
| pool | 10s | 连接池等待超时 |

对于 370MB 的视频，下载时间约 25-30 秒（取决于网速），不会因为总时间过长而超时。

## API

| 接口 | 说明 |
|------|------|
| `POST /api/parse` | 解析链接，返回视频/图文信息 |
| `GET /api/stream?video_url=<url>&quality=1080p` | 流式播放视频 |
| `POST /api/download` | 下载视频/音频/图片 |
| `GET /v1` | 经典暗色版 UI |
| `GET /v2` | WebGL 液态玻璃 UI |


## 下载引擎 (`downloader.py`)

### 平台解析

| 平台 | 解析方式 | 下载方式 |
|------|----------|----------|
| 抖音 | 移动端页面爬取 + Playwright 回退 | httpx 流式下载 |
| Twitter/X | fxtwitter API | 直链优先 → m3u8/ffmpeg 兜底 |
| TikTok | ssstiktok.cc | curl 下载 |
| B站 | bilibili API (fnval=0) | httpx + Referer |
| 快手 | 移动端页面爬取 | httpx (verify=False) |

### 抖音内容类型

| 类型 | 处理方式 |
|------|----------|
| 普通视频 | 正则提取 playwm URL → 去水印 |
| 图文笔记 | 正则提取图片 → 图片浏览 |
| 图文转视频 | ffmpeg 逐张图片转片段 → concat + 音乐 |
| 动图 (live_photo) | Playwright 提取视频+图片 → ffmpeg 合并 |

## 动图（Live Photo）解析

抖音的图文作品分为三种类型：纯图片、动图（Live Photo）、视频。动图是一种特殊的图文作品，包含静态图片和动态视频。

### 识别流程

```
用户粘贴链接
    ↓
解析短链接 → 获取 item_id 和 content_type
    ↓
请求分享页 HTML
    ↓
提取 "images" 数组中的图片 URL
    ↓
检查 "img_bitrate" 字段
    ├── img_bitrate = null → 动图帖（有视频）
    └── img_bitrate = [...] → 纯图片帖（无视频）
    ↓
如果图片 ≤ 1 且是动图帖 → 启动 Playwright 渲染
    ↓
Playwright 加载页面 → 等待图片稳定 → 提取视频和图片
```

### Playwright 提取

抖音的动图帖需要 JavaScript 渲染才能获取完整的视频和图片。Playwright 启动无头浏览器：

1. 加载分享页，等待 `domcontentloaded`
2. 轮询检测图片加载（每 400ms 检查一次，最多 5 秒）
3. 轮询等待视频元素出现（每 400ms 检查一次，最多 3.2 秒）
4. 使用 `page.evaluate` 一次性提取所有 `<video>` 和 `<img>` 元素
5. 过滤规则：
   - 图片：只保留 `tos-cn-i-` 前缀（内容图），排除头像、表情、贴纸、推荐内容
   - 视频：只保留 `douyinvod` 域名的视频 URL（限定在 `<video source>` 元素内）
   - 去重：图片按 ID 去重，视频按路径去重（不同 CDN 节点视为同一视频）
   - 容器范围：图片提取限定在 `.note-detail-container` 内，避免抓取推荐区域
5. 如果有视频 URL → 标记为 `live_photo` 类型

### 返回数据

```json
{
  "type": "live_photo",
  "title": "作品标题",
  "images": ["https://...img1.webp", "https://...img2.webp"],
  "video_url": "https://v.douyinvod.com/...mp4",
  "video_urls": ["https://v11-web.douyinvod.com/...mp4", "https://v26-web.douyinvod.com/...mp4"],
  "music_url": "https://sf6-cdn-tos.douyinstatic.com/...mp3"
}
```

- `images`：所有静态图片 URL
- `video_url`：主视频 URL（用于预览播放）
- `video_urls`：所有视频 URL（多个 CDN 节点，用于下载）

### 动图下载

多动图帖下载时，需要合并多个视频片段：

1. 逐个下载视频片段（需携带 `Referer: https://www.douyin.com/` 头，否则 CDN 返回 403）
2. 过滤无效片段（< 1000 字节或 HTML 错误页）
3. 使用 ffmpeg 重新编码合并（`libx264 + aac`），兼容不同编码参数的片段
4. 输出标准 MP4（`-movflags +faststart`，浏览器可直接播放）

### 清理机制

- **定时清理**：每个文件下载后 10 分钟自动删除（线程定时器）
- **启动清理**：启动时清理超过 30 分钟的旧文件
- **服务器 crontab**：每小时清理 `downloads/` 超过 60 分钟的文件

## 前端特性 (Liquid Glass 3.0)

- **GSAP 动画**：卡片变形（输入→加载水滴→结果）、按钮选中发光旋转
- **鼠标光迹**：50 个弹性跟随点 + 50 个模糊光晕（桌面端）
- **图片滑块**：3+ 张图用 slider 替代 dots，流光动画
- **移动端优化**：响应式布局、触摸目标 44px+、input 16px 防 iOS 缩放
- **移动端滑动条**：等比缩小（活跃圆点 30×21px、非活跃 12×12px、触摸区 40px），动图帖图片 ≥3 张时双行显示
- **下载进度**：fetch + ReadableStream 实时显示百分比
- **版本切换**：右上角菜单一键切换三个 UI

## 技术栈

| 层 | 方案 |
|---|------|
| 后端 | Python FastAPI |
| 下载引擎 | 平台 API + 直接 HTTP 爬取 + Playwright |
| 前端 | HTML + Tailwind CSS + GSAP |
| 设计 | Apple Liquid Glass |
| 视频合成 | ffmpeg |
| 部署 | CentOS + Cloudflare Tunnel |

## 性能优化

### gzip 压缩

FastAPI 启用 `GZipMiddleware`，大于 500 字节的响应自动 gzip 压缩。效果：

| 资源 | 原始大小 | gzip 后 | 压缩率 |
|------|---------|---------|--------|
| html2canvas.min.js | 199KB | ~50KB | 75% |
| tailwindcss.js | 407KB | ~100KB | 75% |
| index.html | 62KB | ~12KB | 80% |

### 静态资源缓存

所有静态资源设置 `Cache-Control` 头：

- JS/CSS/图片：`max-age=604800`（7 天），`immutable`
- HTML 页面：`max-age=600`（10 分钟）

首次加载后，后续访问所有静态资源从浏览器缓存读取，**页面秒开**。

### Tailwind CSS 本地化

`tailwindcss.js` 下载到本地 `static/` 目录，不再依赖 `cdn.tailwindcss.com`（国内访问不稳定）。

### 加载动画

解析结果返回后，显示类型对应的 GSAP 动画图标 + 流光文字：

- 动图（Live Photo）：3 个同心圆（外圈虚线旋转）+ "Live Photo" 流光
- 视频：▶ 播放三角弹入 + "Video" 流光
- 图文：叠加矩形滑入 + "Photo" 流光

## 依赖

- Python 3.9+
- FastAPI + uvicorn + httpx
- Playwright（用于抖音图文/动图解析）
- ffmpeg


## 部署

见 [DEPLOY.md](DEPLOY.md)
