"use strict";
const $ = id => document.getElementById(id);
const statusNames = {paid: "已付款 · 待发货", pending_payment: "待付款", shipped: "运输中", delivered: "已签收", cancelled: "已取消"};
let token = sessionStorage.getItem("commerce_token") || "";
let conversation = crypto.randomUUID();
let busy = false;
let mediaBusy = false;

function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
}

async function api(path, body, method) {
    const binary = body instanceof Blob;
    const response = await fetch(path, {method: method || (body ? "POST" : "GET"),
        headers: {"Content-Type": binary ? body.type : "application/json", ...(token ? {Authorization: `Bearer ${token}`} : {})},
        ...(body ? {body: binary ? body : JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) {
        if (response.status === 401) {
            $("login-overlay").hidden = false;
            $("username").focus();
        }
        throw new Error(data.message || data.detail || "请求失败，请稍后再试");
    }
    return data;
}

function message(text, role = "assistant") {
    const element = node("article", undefined, `message ${role}`);
    element.append(node("div", role === "user" ? "你" : "智能客服", "label"), node("p", text));
    $("messages").append(element);
    $("messages").scrollTop = $("messages").scrollHeight;
    return element;
}

function resetConversation() {
    if (busy || mediaBusy) return;
    if (typeof clearImage === "function") clearImage();
    conversation = crypto.randomUUID();
    $("query").value = "";
    $("status").textContent = "";
    $("messages").replaceChildren();
    message("你好，我可以帮你了解商品、查询订单物流，或核对售后申请条件。\n你可以问“耳机支持降噪吗”或“我的订单”，选择一笔订单后继续追问。");
}

function readableSource(source) {
    const name = String(source.source || "").replace(/^rag:\/\/[^/]+\//, "").split("#")[0];
    return [name, source.version].filter(Boolean).join(" · ") || "知识库资料";
}

async function loadOrders() {
    const {orders} = await api("/orders");
    $("orders").replaceChildren();
    for (const order of orders) {
        const card = node("div", undefined, "order");
        const select = node("button", "咨询这笔订单");
        select.addEventListener("click", () => send(`查询订单 ${order.order_id}`));
        const afterSale = node("button", "查询售后进度");
        afterSale.addEventListener("click", () => send(`查询订单 ${order.order_id} 的售后进度`));
        card.append(node("strong", order.product_name), node("div", statusNames[order.status] || order.status, "small"), node("code", order.order_id), select, afterSale);
        $("orders").append(card);
    }
    if (!orders.length) $("orders").append(node("p", "当前账号暂无订单。", "small"));
}

async function send(query) {
    if (busy || mediaBusy || !query.trim()) return;
    if (!token) { $("login-overlay").hidden = false; return; }
    if (window.matchMedia("(max-width: 780px)").matches) {
        $("sidebar").classList.remove("mobile-open");
        $("mobile-context-toggle").setAttribute("aria-expanded", "false");
        $("mobile-context-toggle").textContent = "订单与商品";
    }
    busy = true;
    if (typeof refreshMediaControls === "function") refreshMediaControls();
    $("send").disabled = true;
    $("new-chat").disabled = true;
    $("query").value = "";
    message(query, "user");
    $("status").textContent = "正在查询资料与核对业务状态…";
    try {
        const result = await api("/chat/", {query, conversation_id: conversation});
        const bubble = message(result.answer);
        if (result.data.orders) {
            for (const order of result.data.orders) {
                const button = node("button", `${order.order_id} · ${order.product_name} · ${statusNames[order.status] || order.status}`);
                button.addEventListener("click", () => {
                    const followup = {
                        after_sale_query: `查询订单 ${order.order_id} 的售后进度`,
                        logistics_query: `查询订单 ${order.order_id} 的物流`,
                        after_sales: `查询订单 ${order.order_id} 的退货条件`,
                        cancel_order: `取消订单 ${order.order_id}`,
                        create_after_sale: `申请退货 ${order.order_id}`,
                    }[result.intent] || `查询订单 ${order.order_id}`;
                    send(followup);
                });
                bubble.append(button);
            }
        }
        if (result.sources?.length) {
            const sources = node("details", undefined, "sources");
            sources.append(node("summary", `参考资料（${result.sources.length}）`));
            const list = node("div", undefined, "source-list");
            result.sources.forEach(source => {
                const item = node("div", undefined, "source-item");
                item.append(node("strong", source.title || "知识库资料"), node("span", readableSource(source)));
                list.append(item);
            });
            sources.append(list);
            bubble.append(sources);
        }
        if (result.status === "confirmation_required") {
            const card = node("div", undefined, "confirm-card");
            const action = result.data.action === "cancel_order" ? "取消订单" : "提交退货申请";
            card.append(node("div", `${action} · ${result.data.order_id} · ${result.data.product_name}`));
            const confirm = node("button", `确认${action}`);
            const expiresAt = Date.now() + result.data.expires_in * 1000;
            confirm.addEventListener("click", async () => {
                if (Date.now() > expiresAt) {confirm.disabled = true; confirm.textContent = "已过期，请重新发起"; return;}
                confirm.disabled = true;
                try {
                    const confirmed = await api("/operations/confirm", {confirmation_id: result.data.confirmation_id});
                    confirm.textContent = "已处理";
                    message(confirmed.message);
                    await loadOrders();
                } catch (error) {
                    confirm.disabled = false;
                    message(error.message);
                }
            });
            card.append(confirm);
            bubble.append(card);
        }
        const trace = node("details", undefined, "trace");
        trace.append(node("summary", `技术信息 · ${result.latency_ms} ms · ${result.request_id.slice(0, 10)}`), node("pre", JSON.stringify(result.tool_trace, null, 2)));
        bubble.append(trace);
    } catch (error) { message(error.message); }
    finally {
        busy = false;
        if (typeof refreshMediaControls === "function") refreshMediaControls();
        $("send").disabled = false;
        $("new-chat").disabled = false;
        $("status").textContent = "";
        $("messages").scrollTop = $("messages").scrollHeight;
    }
}

async function authenticate(register) {
    const credentials = {user_id: $("username").value.trim(), password: $("password").value};
    $("login-error").textContent = "";
    $("login").disabled = $("register").disabled = true;
    try {
        if (register) await api("/auth/register", credentials);
        const result = await api("/auth/login", credentials);
        token = result.access_token;
        sessionStorage.setItem("commerce_token", token);
        $("account-name").textContent = result.user_id;
        $("login-overlay").hidden = true;
        $("password").value = "";
        resetConversation();
        await loadOrders();
        await loadPreferences();
    } catch (error) { $("login-error").textContent = error.message; }
    finally { $("login").disabled = $("register").disabled = false; }
}

async function loadPreferences() {
    const preferences = await api("/preferences");
    $("category").value = preferences.favorite_categories?.[0] || "";
    $("answer-style").value = preferences.answer_style || "concise";
}

$("composer").addEventListener("submit", event => {event.preventDefault(); send($("query").value);});
$("login").addEventListener("click", () => authenticate(false));
$("register").addEventListener("click", () => authenticate(true));
$("password").addEventListener("keydown", event => {if (event.key === "Enter") authenticate(false);});
$("new-chat").addEventListener("click", resetConversation);
$("mobile-context-toggle").addEventListener("click", event => {
    const button = event.currentTarget;
    const mobile = window.matchMedia("(max-width: 780px)").matches;
    if (mobile) {
        const expanded = $("sidebar").classList.contains("mobile-open");
        $("sidebar").classList.toggle("mobile-open", !expanded);
        button.setAttribute("aria-expanded", String(!expanded));
        button.textContent = expanded ? "订单与商品" : "收起订单栏";
    } else {
        const expanded = !$("layout").classList.contains("sidebar-collapsed");
        $("layout").classList.toggle("sidebar-collapsed", expanded);
        button.setAttribute("aria-expanded", String(!expanded));
        button.textContent = expanded ? "显示订单与商品" : "收起订单栏";
    }
});
$("handoff").addEventListener("click", () => send("我要转人工"));
document.querySelectorAll("[data-query]").forEach(button => button.addEventListener("click", () => send(button.dataset.query)));
$("logout").addEventListener("click", async () => {
    if (busy || mediaBusy) return;
    try {if (token) await api("/auth/logout", {});} catch (_) { /* Local cleanup still required. */ }
    token = "";
    sessionStorage.removeItem("commerce_token");
    $("account-name").textContent = "未登录";
    $("orders").replaceChildren();
    resetConversation();
    $("login-overlay").hidden = false;
});
$("save-preferences").addEventListener("click", async () => {
    try {
        await api("/preferences", {favorite_categories: $("category").value ? [$("category").value] : [], answer_style: $("answer-style").value}, "PUT");
        $("preference-status").textContent = "已保存";
    } catch (error) { $("preference-status").textContent = error.message; }
});

async function start() {
    resetConversation();
    if (window.matchMedia("(max-width: 780px)").matches) {
        $("mobile-context-toggle").setAttribute("aria-expanded", "false");
        $("mobile-context-toggle").textContent = "订单与商品";
    }
    if (token) {
        try {
            const me = await api("/auth/me");
            $("account-name").textContent = me.user_id;
            $("login-overlay").hidden = true;
            await loadOrders();
            await loadPreferences();
        } catch (_) {token = ""; sessionStorage.removeItem("commerce_token");}
    }
    if (!token) $("username").focus();
}

$("login-overlay").addEventListener("keydown", event => {
    if (event.key === "Escape") {
        if (token) $("login-overlay").hidden = true;
        return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...$("login-overlay").querySelectorAll("button:not([disabled]), input:not([disabled])")];
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
start();
