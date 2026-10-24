import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import prettier from "eslint-config-prettier";
import globals from "globals";
import { createConfig } from "./.build/jpt/configs/eslint.mjs";
export default createConfig({ js, defineConfig, prettier, globals });
