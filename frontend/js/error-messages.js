// Convert backend error codes into messages suitable for end users.
window.AGENT_ERROR_MESSAGES = Object.freeze({
    VALIDATION_ERROR: "请检查输入内容后再提交。",
    HTTP_401: "登录状态已失效，请重新登录。",
    HTTP_403: "您暂无权限执行此操作。",
    HTTP_404: "请求的内容不存在。",
    HTTP_409: "当前请求与已有数据冲突，请检查后重试。",
    HTTP_429: "请求过于频繁，请稍后再试。",
    INTERNAL_SERVER_ERROR: "系统暂时繁忙，请稍后重试。",
});

function getAgentErrorMessage(data, status, fallback = "请求失败") {
    const code = data && typeof data.code === "string" ? data.code : "";
    const mappedMessage = window.AGENT_ERROR_MESSAGES[code];
    if (mappedMessage) return mappedMessage;

    if (data && typeof data.message === "string" && data.message.trim()) {
        return data.message.trim();
    }

    return `${fallback}（HTTP ${status}）`;
}
