// Export a code-native SVG composition; original character paths are unchanged.
// Usage: node docs/pet-animation/export-reference.cjs [absolute node_modules directory]
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const sharp = require(require.resolve('sharp', {paths:[process.argv[2] || process.cwd()]}));
const source = path.resolve(__dirname, '../../frontend/assets/pet/tuntun-cool.svg');
const original = fs.readFileSync(source, 'utf8');
const output = path.join(__dirname, 'reference');
fs.mkdirSync(output, {recursive:true});
// Visible alpha bounds measured from the existing 1280px export, in 640px SVG units.
const bounds = {left:77, top:34.5, right:580, bottom:596.5};
const scale = (1080 * .65) / (bounds.bottom - bounds.top);
const x = 960 - (bounds.left + bounds.right) / 2 * scale;
const y = 1080 * .2 - bounds.top * scale;
const inner = original.replace(/<svg\b[^>]*>/, '').replace(/<\/svg>\s*$/, '');
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080"><rect width="1920" height="1080" fill="#00ff00"/><g transform="translate(${x} ${y}) scale(${scale})" fill="none">${inner}</g></svg>`;
fs.writeFileSync(path.join(output,'tuntun-start-end.svg'), svg);
fs.writeFileSync(path.join(output,'provenance.json'), JSON.stringify({source:'frontend/assets/pet/tuntun-cool.svg',sha256:crypto.createHash('sha256').update(original).digest('hex'),method:'Original SVG paths rendered on a uniform green canvas with sharp/libvips; no generative redraw.',width:1920,height:1080,green:'#00FF00',source_bounds:bounds,scale,translate:[x,y]},null,2)+'\n');
sharp(Buffer.from(svg)).png().toFile(path.join(output,'tuntun-start-end.png')).then(() => console.log('Exported exact SVG reference to '+output));
