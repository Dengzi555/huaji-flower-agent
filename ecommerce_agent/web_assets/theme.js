/* 提前设置主题，减少首屏闪白；深色主题使用独立亮金SVG，偏好保存在本机。 */
(()=>{
  let saved=null;
  try{saved=localStorage.getItem("flower-theme");}catch{}
  let theme=saved==="dark"||saved==="light" ? saved : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const apply=()=>{
    document.documentElement.dataset.theme=theme;
    document.documentElement.style.colorScheme=theme;
    const source=theme==="dark" ? "/brand-mark-dark.svg" : "/brand-mark.svg";
    for(const img of document.querySelectorAll('img[src="/brand-mark.svg"],img[src="/brand-mark-dark.svg"]'))img.src=source;
    const favicon=document.querySelector('link[rel="icon"]');if(favicon)favicon.href=source;
    const button=document.getElementById("theme-toggle");
    if(button){button.textContent=theme==="dark" ? "☀ 明亮" : "☾ 暗色";button.setAttribute("aria-label",theme==="dark" ? "切换为明亮主题" : "切换为暗色主题");button.setAttribute("aria-pressed",String(theme==="dark"));}
  };
  apply();
  document.addEventListener("DOMContentLoaded",()=>{
    apply();document.getElementById("theme-toggle").addEventListener("click",()=>{theme=theme==="dark"?"light":"dark";try{localStorage.setItem("flower-theme",theme);}catch{}apply();});
  });
})();
