/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // Docker 用：只把實際 import 到的檔案輸出到 .next/standalone，
  // 最終 image 不需要整包 node_modules(約 500MB -> 150MB)。
  // Dockerfile 的 runner 階段就是複製這個目錄，拿掉這行會 build 失敗。
  output: "standalone",

  experimental: {
    // pg 內部會動態 require 選配的原生模組(pg-native / pg-cloudflare)。
    // 讓 Next 不要把它打包進 bundle，改成執行時直接 require，
    // 否則 build 可能出現 "Module not found: Can't resolve 'pg-native'"。
    serverComponentsExternalPackages: ["pg"],
  },
};

export default nextConfig;
