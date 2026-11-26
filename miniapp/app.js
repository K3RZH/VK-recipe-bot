const form = document.querySelector("#recipe-form");
const input = document.querySelector("#ingredients");
const log = document.querySelector("#chat-log");
const status = document.querySelector("#recipe-status");
const send = document.querySelector("#recipe-send");

const launch = new URLSearchParams(window.location.search);
if (launch.has("vk_app_id") && window.vkBridge) {
  window.vkBridge.send("VKWebAppInit").catch(() => {
    status.textContent = "Приложение не смогло связаться с ВК. Подбор можно попробовать здесь.";
  });
}

function addMessage(text, fromUser = false) {
  const message = document.createElement("p");
  message.className = fromUser ? "chat__message chat__message--user" : "chat__message";
  if (fromUser) {
    message.textContent = text;
  } else {
    const lines = text.split("\n");
    for (const [index, line] of lines.entries()) {
      if (index) message.append("\n");
      let url;
      try {
        if (lines[index - 1]?.startsWith("Источник: ") && line.startsWith("https://")) {
          url = new URL(line);
        }
      } catch {
        url = null;
      }
      if (!url || url.username || url.password || url.port) {
        message.append(line);
        continue;
      }
      const link = document.createElement("a");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Открыть оригинал";
      message.append(link);
    }
  }
  log.append(message);
  log.scrollTop = message.offsetTop - 16;
}

function addPhoto(photo) {
  const figure = document.createElement("figure");
  figure.className = "chat__photo";
  const image = document.createElement("img");
  image.className = "chat__photo-image";
  image.src = photo.src;
  image.alt = photo.title;
  image.referrerPolicy = "no-referrer";
  if (Number.isInteger(photo.width) && Number.isInteger(photo.height)) {
    image.width = photo.width;
    image.height = photo.height;
  }
  image.addEventListener("error", () => figure.remove(), { once: true });

  const caption = document.createElement("figcaption");
  caption.className = "chat__photo-caption";
  caption.append("Пример подачи: " + photo.title, document.createElement("br"),
    photo.author + ", ");
  for (const [href, label] of [[photo.page, "Wikimedia Commons"], [photo.license_url, photo.license]]) {
    const link = document.createElement("a");
    link.href = href;
    link.textContent = label;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    if (href === photo.license_url) caption.append(" · ");
    caption.append(link);
  }
  figure.append(image, caption);
  log.append(figure);
  return figure;
}

for (const button of document.querySelectorAll("[data-example]")) {
  button.addEventListener("click", () => {
    input.value = button.dataset.example;
    input.focus();
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (send.disabled) return;

  const message = input.value.trim();
  if (!message) {
    status.textContent = "Напиши, какие продукты у тебя есть.";
    input.focus();
    return;
  }

  send.disabled = true;
  status.textContent = "Ищем рецепты на сайтах…";

  try {
    const response = await fetch("/api/recipes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const result = await response.json();
    if (!response.ok) {
      status.textContent = result.error;
      return;
    }
    addMessage(message, true);
    const figures = (result.photos ?? []).map(addPhoto);
    addMessage(result.answer);
    if (figures.length) log.scrollTop = figures[0].offsetTop - 16;
    if (input.value.trim() === message) input.value = "";
    status.textContent = "";
  } catch {
    status.textContent = "Связь прервалась. Попробуй отправить запрос ещё раз.";
  } finally {
    send.disabled = false;
  }
});
