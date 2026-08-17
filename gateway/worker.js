export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const authHeader = request.headers.get("Authorization") || "";
    const token = authHeader.replace(/^Bearer\s+/i, "").trim();

    // 清洗环境变量（去除首尾空格、外层引号、以及 TG_BOT_TOKEN 的重复 bot 前缀）
    const sanitize = (val) => (val || "").toString().trim().replace(/^["']|["']$/g, "");
    const tgBotToken = sanitize(env.TG_BOT_TOKEN).replace(/^bot/i, "");
    const tgChatId = sanitize(env.TG_CHAT_ID);
    const ghToken = sanitize(env.GH_TOKEN);
    const gatewayAuthKey = sanitize(env.GATEWAY_AUTH_KEY);

    // 校验网关通信密钥
    if (gatewayAuthKey && token !== gatewayAuthKey) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/json" }
      });
    }

    // 1. Telegram 消息代理
    if (url.pathname === "/api/tg" && request.method === "POST") {
      if (!tgBotToken || !tgChatId) {
        return new Response(JSON.stringify({
          ok: false,
          error: "TG_BOT_TOKEN or TG_CHAT_ID is not configured in Cloudflare Worker environment/secrets"
        }), {
          status: 500,
          headers: { "content-type": "application/json" }
        });
      }
      const body = await request.json();
      const tgUrl = `https://api.telegram.org/bot${tgBotToken}/sendMessage`;
      const resp = await fetch(tgUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_id: tgChatId,
          text: body.text,
          parse_mode: body.parse_mode || "Markdown"
        })
      });
      return new Response(await resp.text(), {
        status: resp.status,
        headers: { "content-type": "application/json" }
      });
    }

    // 2. GitHub Actions Dispatch 触发代理
    if (url.pathname === "/api/gh-dispatch" && request.method === "POST") {
      if (!ghToken) {
        return new Response(JSON.stringify({
          ok: false,
          error: "GH_TOKEN is not configured in Cloudflare Worker environment/secrets"
        }), {
          status: 500,
          headers: { "content-type": "application/json" }
        });
      }
      const body = await request.json();
      const ghUrl = "https://api.github.com/repos/svbmwjwj/snack-connoisseur/dispatches";
      const resp = await fetch(ghUrl, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github.v3+json",
          "Authorization": `Bearer ${ghToken}`,
          "User-Agent": "Snack-Gateway"
        },
        body: JSON.stringify({
          event_type: "scan_trigger",
          client_payload: body
        })
      });
      return new Response(await resp.text(), {
        status: resp.status,
        headers: { "content-type": "application/json" }
      });
    }

    // 3. 脚本模板安全拉取代理
    if (url.pathname.startsWith("/api/raw/")) {
      const file = url.pathname.replace("/api/raw/", "");
      const rawUrl = `https://raw.githubusercontent.com/svbmwjwj/snack-connoisseur/main/templates/${file}`;
      const headers = { "User-Agent": "Snack-Gateway" };
      if (ghToken) {
        headers["Authorization"] = `token ${ghToken}`;
      }
      const resp = await fetch(rawUrl, { headers });
      return new Response(await resp.text(), {
        status: resp.status,
        headers: { "content-type": "text/plain; charset=utf-8" }
      });
    }

    // 4. KV / Storage 产物存储与拉取代理 (支持直接存取与 Release 回退)
    if (url.pathname.startsWith("/api/storage/") || url.pathname.startsWith("/api/release/")) {
      const filename = url.pathname.replace(/^\/api\/(storage|release)\//, "");
      const kv = env.CONNOISSEUR_KV || env.CONNOISSEUR_DATA_KV || env.SNACK_CONNOISSEUR_KV || env.SNACK_KV || env["snack-connoisseur_data"];
      
      // A. 上传存储 (PUT /api/storage/:filename)
      if (request.method === "PUT") {
        if (kv) {
          const content = await request.text();
          await kv.put(`csv:${filename}`, content, { expirationTtl: 86400 * 30 }); // 30天自动过期
          return new Response(JSON.stringify({ ok: true, filename }), {
            status: 200,
            headers: { "content-type": "application/json" }
          });
        }
        return new Response(JSON.stringify({ error: "KV namespace is not bound" }), {
          status: 500,
          headers: { "content-type": "application/json" }
        });
      }

      // B. 优先从 KV 缓存中直接高速私密读取
      if (kv) {
        const cachedCsv = await kv.get(`csv:${filename}`);
        if (cachedCsv) {
          return new Response(cachedCsv, {
            status: 200,
            headers: { "content-type": "text/csv; charset=utf-8" }
          });
        }
      }

      // C. 回退：从 GitHub Release 中拉取
      if (!ghToken) {
        return new Response(JSON.stringify({
          ok: false,
          error: "Asset not found in KV and GH_TOKEN is not configured"
        }), {
          status: 500,
          headers: { "content-type": "application/json" }
        });
      }
      const releaseUrl = "https://api.github.com/repos/svbmwjwj/snack-connoisseur/releases/tags/latest";
      const relResp = await fetch(releaseUrl, {
        headers: {
          "Accept": "application/vnd.github.v3+json",
          "Authorization": `token ${ghToken}`,
          "User-Agent": "Snack-Gateway"
        }
      });
      if (!relResp.ok) {
        return new Response(`Release lookup failed: ${relResp.statusText}`, { status: relResp.status });
      }
      const relData = await relResp.json();
      const targetAsset = (relData.assets || []).find((a) => a.name === filename);
      if (!targetAsset) {
        return new Response(`Asset ${filename} not found`, { status: 404 });
      }
      const assetResp = await fetch(targetAsset.url, {
        headers: {
          "Accept": "application/octet-stream",
          "Authorization": `token ${ghToken}`,
          "User-Agent": "Snack-Gateway"
        },
        redirect: "manual"
      });
      let finalResp = assetResp;
      if (assetResp.status === 301 || assetResp.status === 302) {
        const location = assetResp.headers.get("Location");
        finalResp = await fetch(location, {
          headers: {
            "User-Agent": "Snack-Gateway"
          }
        });
      }
      return new Response(await finalResp.text(), {
        status: finalResp.status,
        headers: {
          "content-type": "text/csv; charset=utf-8"
        }
      });
    }

    return new Response(JSON.stringify({
      status: "Snack Gateway Running",
      secrets: {
        TG_BOT_TOKEN: !!tgBotToken,
        TG_CHAT_ID: !!tgChatId,
        GH_TOKEN: !!ghToken,
        GATEWAY_AUTH_KEY: !!gatewayAuthKey
      }
    }), {
      headers: { "content-type": "application/json" }
    });
  }
};
