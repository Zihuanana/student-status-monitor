/// <reference types="vite/client" />

// 静态资源导入的类型声明。
// 没有它，`import x from './a.svg'` 会让 tsc 报 TS2307
// （Vite 能处理，但 TypeScript 不知道这类模块）。
declare module '*.svg' {
  const src: string
  export default src
}
declare module '*.png' {
  const src: string
  export default src
}
declare module '*.jpg' {
  const src: string
  export default src
}
declare module '*.webp' {
  const src: string
  export default src
}
