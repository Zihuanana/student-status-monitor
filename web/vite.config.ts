import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 静态资源的引用前缀。
//
// ⚠ 这里踩过一次**白屏**：原来写死成云端的隐藏路径
//   （/3425jh3g…/），结果迁到内网挂 /monitor/ 后，HTML 里的
//   <script src="/3425jh3g…/assets/index-xxx.js"> 指向不存在的路径 →
//   JS 加载 404 → <div id="root"> 永远空白。
//   而服务端返回 200、接口也正常，光看状态码发现不了。
//
// 现在默认用**相对路径**（'./'）：构建产物放到任何前缀下都能工作，
// 不需要为了换部署路径重新构建。
// 需要绝对路径时（例如要部署到 CDN 子目录）显式设 APP_BASE_PATH 即可。
const APP_BASE_PATH = process.env.APP_BASE_PATH || ''

export default defineConfig({
  plugins: [react()],
  base: APP_BASE_PATH ? `/${APP_BASE_PATH.replace(/^\/|\/$/g, '')}/` : './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    // 部署体积敏感（服务器仅 11G 可用），关掉 sourcemap
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8002', changeOrigin: true },
    },
  },
})
