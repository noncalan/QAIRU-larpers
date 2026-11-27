import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./": сборка работает из любой подпапки (GitHub Pages /QAIRU-larpers-/, локальный файл-сервер)
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", assetsInlineLimit: 0 },
});
