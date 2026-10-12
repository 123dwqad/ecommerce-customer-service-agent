"use strict";
let mediaOptions = {speech: {ready: false}, vision: {ready: false}};
let selectedImage = null;
let imageObjectUrl = null;
let recorder = null;
let microphone = null;
let recordingTimer = null;
let recordingInterval = null;
let recordingStarted = 0;
let cancellingRecording = false;

function refreshMediaControls() {
    const recording = recorder?.state === "recording";
    $("record-audio").disabled = busy || (!recording && (mediaBusy || !mediaOptions.speech.ready));
    $("record-audio").textContent = recording ? "停止并转写" : "语音输入";
    $("cancel-recording").hidden = !recording;
    $("choose-image").disabled = busy || mediaBusy || !mediaOptions.vision.ready;
    $("analyze-image").disabled = busy || mediaBusy || !selectedImage || !mediaOptions.vision.ready;
    $("remove-image").disabled = busy || mediaBusy;
    $("send").disabled = busy || mediaBusy;
    $("new-chat").disabled = busy || mediaBusy;
    $("logout").disabled = busy || mediaBusy;
}

function mediaLock(locked) {
    mediaBusy = locked;
    refreshMediaControls();
}

function clearImage() {
    selectedImage = null;
    if (imageObjectUrl) URL.revokeObjectURL(imageObjectUrl);
    imageObjectUrl = null;
    $("image-preview").removeAttribute("src");
    $("image-panel").hidden = true;
    $("image-file").value = "";
    $("image-question").value = "";
    refreshMediaControls();
}

function needLogin() {
    if (token) return false;
    $("login-overlay").hidden = false;
    return true;
}

async function toWav(blob) {
    if (blob.size > 12 * 1024 * 1024) throw new Error("录音数据过大，请缩短录音后重试。");
    const Audio = window.AudioContext || window.webkitAudioContext;
    const OfflineAudio = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!Audio || !OfflineAudio) throw new Error("浏览器不支持音频解码，请使用新版 Chrome 或 Edge。");
    const decoder = new Audio();
    try {
        const decoded = await decoder.decodeAudioData(await blob.arrayBuffer());
        if (decoded.duration < 0.1 || decoded.duration > 60.1) throw new Error("音频时长需在 0.1 至 60 秒之间。");
        const length = Math.min(960000, Math.floor(decoded.duration * 16000));
        const offline = new OfflineAudio(1, length, 16000);
        const source = offline.createBufferSource();
        source.buffer = decoded;
        source.connect(offline.destination);
        source.start();
        const samples = (await offline.startRendering()).getChannelData(0);
        const buffer = new ArrayBuffer(44 + samples.length * 2);
        const view = new DataView(buffer);
        const text = (offset, value) => {for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));};
        text(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
        text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
        view.setUint16(22, 1, true); view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
        view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, "data");
        view.setUint32(40, samples.length * 2, true);
        for (let i = 0; i < samples.length; i++) {
            const sample = Math.max(-1, Math.min(1, samples[i]));
            view.setInt16(44 + i * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
        }
        return new Blob([buffer], {type: "audio/wav"});
    } finally {await decoder.close();}
}

async function transcribe(blob) {
    mediaLock(true);
    $("status").textContent = "正在转写语音…";
    try {
        const wav = await toWav(blob);
        const result = await api("/media/transcribe", wav);
        // ASR output is only a draft; never auto-send it to business execution.
        const existing = $("query").value.trim();
        const combined = existing ? existing + " " + result.text : result.text;
        if (combined.length > 1500) {
            message("转写结果：" + result.text + "\n输入框已有内容，请自行整理后发送。");
        } else {
            $("query").value = combined;
            $("query").focus();
        }
        $("status").textContent = "转写完成，请检查文字后点击发送。";
    } catch (error) {$("status").textContent = error.message || "音频无法识别，请重试。";}
    finally {mediaLock(false);}
}

function releaseMicrophone() {
    clearTimeout(recordingTimer);
    clearInterval(recordingInterval);
    microphone?.getTracks().forEach(track => track.stop());
    microphone = null;
}

async function toggleRecording() {
    if (recorder?.state === "recording") {recorder.stop(); return;}
    if (busy || mediaBusy || needLogin()) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        $("status").textContent = "语音录音需要 HTTPS 或 localhost，以及支持麦克风的浏览器。";
        return;
    }
    mediaLock(true);
    cancellingRecording = false;
    try {
        microphone = await navigator.mediaDevices.getUserMedia({audio: true});
        const mimeType = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(type => MediaRecorder.isTypeSupported(type));
        recorder = new MediaRecorder(microphone, mimeType ? {mimeType} : {});
        const chunks = [];
        recorder.ondataavailable = event => {if (event.data.size) chunks.push(event.data);};
        recorder.onerror = () => {
            cancellingRecording = true;
            releaseMicrophone();
            if (recorder?.state === "recording") recorder.stop();
            $("status").textContent = "录音失败，请检查麦克风后重试。";
            mediaLock(false);
        };
        recorder.onstop = async () => {
            const type = recorder.mimeType;
            releaseMicrophone();
            recorder = null;
            if (cancellingRecording) {mediaLock(false); return;}
            await transcribe(new Blob(chunks, {type}));
        };
        recordingStarted = Date.now();
        recorder.start();
        // Leave headroom for browser timer and encoder latency.
        recordingTimer = setTimeout(() => {if (recorder?.state === "recording") recorder.stop();}, 59000);
        recordingInterval = setInterval(() => {$("status").textContent = `正在录音 ${Math.floor((Date.now()-recordingStarted)/1000)} 秒，停止后发送至语音服务转写。`;}, 250);
        refreshMediaControls();
    } catch (error) {
        releaseMicrophone();
        recorder = null;
        mediaLock(false);
        $("status").textContent = error.name === "NotAllowedError" ? "未获得麦克风权限，请在浏览器设置中允许后重试。" : "无法打开麦克风，请检查设备后重试。";
    }
}

$("record-audio").addEventListener("click", toggleRecording);
$("cancel-recording").addEventListener("click", () => {
    cancellingRecording = true;
    if (recorder?.state === "recording") recorder.stop();
    $("status").textContent = "已取消录音，未上传。";
});
$("choose-image").addEventListener("click", () => {if (!needLogin()) $("image-file").click();});
$("image-file").addEventListener("change", event => {
    const file = event.target.files[0];
    if (!file || busy || mediaBusy) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
        $("status").textContent = "请选择 5 MB 以内的 JPEG、PNG 或 WebP 图片。";
        event.target.value = "";
        return;
    }
    clearImage();
    selectedImage = file;
    imageObjectUrl = URL.createObjectURL(file);
    $("image-preview").src = imageObjectUrl;
    $("image-panel").hidden = false;
    $("status").textContent = "图片仅在本地预览，点击“识别图片”后才会上传。";
    refreshMediaControls();
});
$("remove-image").addEventListener("click", clearImage);
$("analyze-image").addEventListener("click", async () => {
    if (!selectedImage || busy || mediaBusy || needLogin()) return;
    mediaLock(true);
    $("status").textContent = "正在识别图片…";
    try {
        const encoded = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(",")[1]);
            reader.onerror = () => reject(new Error("图片读取失败。"));
            reader.readAsDataURL(selectedImage);
        });
        const question = $("image-question").value.trim() || "请描述图片中商品的外观、可见文字或包装问题。";
        const result = await api("/media/vision", {image_base64: encoded, question});
        const bubble = message("图片识别：\n" + result.answer);
        bubble.append(node("div", result.notice, "sources"));
        $("status").textContent = "识别完成。若需查询售后，请另行提供本人订单号。";
        clearImage();
    } catch (error) {$("status").textContent = error.message;}
    finally {mediaLock(false);}
});
window.addEventListener("pagehide", () => {
    cancellingRecording = true;
    if (recorder?.state === "recording") recorder.stop();
    releaseMicrophone();
    if (imageObjectUrl) URL.revokeObjectURL(imageObjectUrl);
});

async function loadMediaOptions() {
    try {
        mediaOptions = await api("/media/options");
        $("media-config-status").textContent = `语音${mediaOptions.speech.ready ? "可用" : "未配置"} · 图片${mediaOptions.vision.ready ? "可用" : "未配置"}`;
    } catch (_) {$("media-config-status").textContent = "多模态服务暂不可用";}
    refreshMediaControls();
}
loadMediaOptions();
