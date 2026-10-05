const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu']
  });

  const items = [
    { html: 'category_knowledge.html', output: '知识.png' },
    { html: 'category_cardkey.html', output: '卡密.png' },
    { html: 'category_resources.html', output: '资源.png' },
    { html: 'category_privileges.html', output: '权益.png' },
  ];

  const staticDir = 'c:\\Users\\笨猫\\Desktop\\MT4-Ai\\src\\web\\static';
  const desktopDir = 'c:\\Users\\笨猫\\Desktop';

  for (const item of items) {
    const page = await browser.newPage();
    await page.setViewport({ width: 750, height: 420, deviceScaleFactor: 2 });
    const filePath = path.join(staticDir, item.html);
    const fileUrl = 'file:///' + filePath.replace(/\\/g, '/');
    await page.goto(fileUrl, { waitUntil: 'networkidle0' });
    await new Promise(r => setTimeout(r, 500));
    const outputPath = path.join(desktopDir, item.output);
    await page.screenshot({ path: outputPath, type: 'png', clip: { x: 0, y: 0, width: 750, height: 420 } });
    console.log('Saved:', outputPath);
    await page.close();
  }

  await browser.close();
  console.log('Done!');
})();
