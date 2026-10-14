const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const html = fs.readFileSync(process.argv[2] || path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const sources = [];
let userGesture = false;
class Element {
  constructor(tag, root = false) {
    this.tagName = tag.toLowerCase(); this.root = root; this.children = [];
    this.parentNode = null;
    this.style = { setProperty(key, value) { this[key] = value; } };
    this.dataset = {}; this.attrs = {};
    this.listeners = {}; this.hidden = false; this.clientWidth = 400; this.clientHeight = 700;
    const classes = new Set();
    this.classList = {
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, active) { if (active ?? !classes.has(name)) classes.add(name); else classes.delete(name); },
    };
    this.paused = true; this.ended = false; this.currentTime = 0; this.duration = 120;
    this.playbackRate = 1; this.defaultPlaybackRate = 1; this.volume = 1; this.muted = false;
  }
  get isConnected() { return this.root || !!(this.parentNode && this.parentNode.isConnected); }
  set innerHTML(value) { this.children.forEach(child => child.parentNode = null); this.children = []; }
  appendChild(child) {
    if (child.tagName === 'fragment') {
      [...child.children].forEach(item => this.appendChild(item)); child.children = []; return child;
    }
    child.remove(); child.parentNode = this; this.children.push(child); return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
    this.parentNode = null;
  }
  insertBefore(child, reference) {
    child.remove();
    const position = reference ? this.children.indexOf(reference) : this.children.length;
    child.parentNode = this; this.children.splice(position, 0, child); return child;
  }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  getAttribute(key) { return this.attrs[key] || null; }
  set src(value) { this.attrs.src = value; sources.push({ tag: this.tagName, url: value }); }
  get src() { return this.attrs.src || ''; }
  addEventListener(type, listener) { (this.listeners[type] ||= new Set()).add(listener); }
  removeEventListener(type, listener) { this.listeners[type]?.delete(listener); }
  emit(type, event = {}) {
    const previousGesture = userGesture;
    userGesture = ['click', 'dblclick', 'touchend', 'keydown'].includes(type);
    try { [...(this.listeners[type] || [])].forEach(listener => listener({target: this, ...event})); }
    finally { userGesture = previousGesture; }
  }
  pause() { this.paused = true; }
  async play() {
    if (this.requireUserGesture && !userGesture && !this.muted) {
      throw Object.assign(new Error('Audible playback requires a direct gesture'), {name: 'NotAllowedError'});
    }
    this.paused = false; this.ended = false;
  }
  load() { this.loadCalls = (this.loadCalls || 0) + 1; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  getBoundingClientRect() { return {left: 0, top: 0, width: this.clientWidth, height: this.clientHeight}; }
  querySelectorAll(selector) {
    const result = [];
    const matches = element => ['img', 'video'].includes(selector) ? element.tagName === selector :
      element.tagName === 'img' && Object.prototype.hasOwnProperty.call(element.dataset, 'src');
    const visit = element => element.children.forEach(child => {
      if (matches(child)) result.push(child); visit(child);
    });
    visit(this); return result;
  }
}
const ids = new Map();
const document = {
  getElementById(id) {
    if (!ids.has(id)) ids.set(id, new Element('div', true));
    return ids.get(id);
  },
  createElement(tag) { return new Element(tag); },
  createDocumentFragment() { return new Element('fragment'); },
  addEventListener() {}, body: new Element('body', true),
};
let observer, timerId = 0;
const timers = new Map();
class Observer {
  constructor(callback) { this.callback = callback; this.elements = new Set(); observer = this; }
  observe(element) { this.elements.add(element); }
  unobserve(element) { this.elements.delete(element); }
  disconnect() { this.elements.clear(); }
  visible() { this.callback([...this.elements].map(target => ({ target, isIntersecting: true }))); }
}
const context = vm.createContext({
  document, window: { IntersectionObserver: Observer, addEventListener() {}, scrollTo() {},
    innerHeight: 800, scrollY: 0 }, IntersectionObserver: Observer,
  fetch: () => new Promise(() => {}), location: { replace() {} },
  setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
  clearTimeout: id => timers.delete(id), console,
});
vm.runInContext(script, context);
const run = code => vm.runInContext(code, context);

async function main() {
  run(`lbItems = [0,1,2].map(i => ({type:'video',root:0,path:'clip'+i+'.mp4'}));
       lbIndex=1; lb.hidden=false; renderSlides();`);
  let videos = ids.get('lbTrack').querySelectorAll('video');
  assert.equal(videos.length, 3);
  assert.equal(videos.filter(video => video.src).length, 1);
  assert(videos[1].src.includes('clip1.mp4'));
  assert.equal(videos[0].preload, 'none');
  run('closeLightbox()');
  assert(videos.every(video => !video.src && video.paused));
  console.log('PASS: only the selected video loads; closing releases video sources');

  run(`view={mode:'folder',root:0,path:''};
       openLightbox([0,1,2,3].map(i=>({type:'image',root:0,path:'photo'+i+'.jpg',name:'photo'})),1);`);
  const previous = run('lbSlides.get(0).media');
  const selected = run('lbSlides.get(1).media');
  const neighbor = run('lbSlides.get(2).media');
  selected.naturalWidth = 1600; selected.naturalHeight = 900;
  const sourceCount = sources.length;
  run('rotateCurrent(); resizeLightbox();');
  assert.strictEqual(run('lbSlides.get(1).media'), selected);
  assert.equal(selected.style.transform, 'rotate(90deg)');
  assert.equal(parseFloat(selected.style.width), 700);
  assert.equal(parseFloat(selected.style.height), 393.75);
  assert.equal(sources.length, sourceCount);
  run('go(1); go(1); finishSwitch();');
  assert.equal(run('lbIndex'), 2);
  assert.strictEqual(run('lbSlides.get(2).media'), neighbor);
  assert.strictEqual(run('lbSlides.get(1).media'), selected);
  assert.equal(sources.length, sourceCount + 1);
  assert.equal(previous.src, '');
  run('go(-1); finishSwitch();');
  assert.strictEqual(run('lbSlides.get(1).media'), selected);
  assert.equal(selected.style.transform, 'rotate(90deg)');
  console.log('PASS: rotation fits the viewport; adjacent images survive switching and resizing');

  run('go(1)');
  const staleCallback = timers.get(run('lbSwitchTimer')).callback;
  run(`closeLightbox(); openLightbox([{type:'image',root:0,path:'different.jpg'}],0);`);
  staleCallback();
  assert.equal(run('lbIndex'), 0);
  assert.equal(run('lbItems[0].path'), 'different.jpg');
  assert.equal(run('lbTrack.children.length'), 3);
  console.log('PASS: an old slide animation cannot change a newly opened item');

  run(`openLightbox([0,1,2].map(i=>({type:'image',root:0,path:'swipe'+i+'.jpg'})),0);`);
  const viewport = ids.get('lbViewport');
  let prevented = false;
  viewport.emit('touchstart', {touches:[{clientX:300,clientY:300}]});
  viewport.emit('touchmove', {touches:[{clientX:100,clientY:305}],cancelable:true,
    preventDefault(){ prevented = true; }});
  viewport.emit('touchend');
  run('finishSwitch()');
  assert.equal(run('lbIndex'), 1);
  assert(prevented);
  viewport.emit('touchstart', {touches:[{clientX:300,clientY:300}]});
  viewport.emit('touchmove', {touches:[{clientX:100,clientY:305}],cancelable:true,preventDefault(){}});
  viewport.emit('touchcancel');
  assert.equal(run('lbIndex'), 1);
  assert.equal(ids.get('lbTrack').style.transform, 'translateX(-400px)');
  console.log('PASS: horizontal touch switches items; cancelled gestures return to the current item');

  run(`openLightbox([0,1,2].map(i=>({type:'video',root:0,path:'video'+i+'.mp4'})),1);`);
  const playing = run('lbSlides.get(1).media');
  playing.videoWidth = 1920; playing.videoHeight = 1080; playing.paused = false;
  playing.currentTime = 12;
  const videoSources = sources.filter(source=>source.tag==='video').length;
  run('rotateCurrent(); resizeLightbox();');
  assert.strictEqual(run('lbSlides.get(1).media'), playing);
  assert.equal(playing.paused, false);
  assert.equal(playing.currentTime, 12);
  assert.equal(playing.loadCalls || 0, 0);
  assert.equal(sources.filter(source=>source.tag==='video').length, videoSources);
  run('go(1); finishSwitch();');
  assert.equal(playing.paused, true);
  assert.equal(playing.src, '');
  assert.equal(ids.get('lbTrack').querySelectorAll('video').filter(video=>video.src).length, 1);
  console.log('PASS: video playback survives rotation/resize; switching releases the old download');

  const lightbox = ids.get('lightbox');
  lightbox.requestFullscreen = async () => { document.fullscreenElement = lightbox; };
  document.exitFullscreen = async () => { document.fullscreenElement = null; };
  await run('toggleFullscreen()');
  assert.strictEqual(document.fullscreenElement, lightbox);
  assert.equal(ids.get('lbFullscreen').attrs['aria-pressed'], 'true');
  run('closeLightbox()');
  assert.equal(document.fullscreenElement, null);
  delete lightbox.requestFullscreen;
  run(`openLightbox([{type:'video',root:0,path:'native.mp4'}],0)`);
  const native = run('lbSlides.get(0).media');
  native.readyState = 1;
  let unrotatedNativeCalls = 0;
  native.webkitEnterFullscreen = () => { unrotatedNativeCalls++; native.webkitDisplayingFullscreen = true; };
  native.webkitExitFullscreen = () => { native.webkitDisplayingFullscreen = false; native.emit('webkitendfullscreen'); };
  await run('toggleFullscreen()');
  assert(lightbox.classList.contains('immersive'));
  assert.equal(unrotatedNativeCalls, 0);
  native.webkitDisplayingFullscreen = false;
  native.emit('webkitendfullscreen');
  lightbox.requestFullscreen = async () => { throw new Error('unsupported'); };
  await run('toggleFullscreen()');
  assert(lightbox.classList.contains('immersive'));
  assert.equal(unrotatedNativeCalls, 0);
  delete lightbox.requestFullscreen;
  run('closeLightbox()');
  assert(!native.webkitDisplayingFullscreen);
  run(`openLightbox([{type:'image',root:0,path:'fallback.jpg'}],0)`);
  await run('toggleFullscreen()');
  assert.equal(ids.get('lbNotice').hidden, false);
  assert(ids.get('lbNotice').textContent.includes('主屏幕'));
  run('closeLightbox()');
  console.log('PASS: browser fullscreen and iPhone page fullscreen preserve the custom gesture surface');

  run(`openLightbox([{type:'video',root:0,path:'iphone-rotated.mp4'}],0); rotateCurrent();`);
  const rotated = run('currentVideo()');
  rotated.videoWidth = 1920; rotated.videoHeight = 1080;
  rotated.readyState = 4; rotated.currentTime = 42; rotated.paused = false;
  let nativeCalls = 0;
  rotated.webkitEnterFullscreen = () => { nativeCalls++; };
  const rotatedSources = sources.length;
  await run('toggleFullscreen()');
  assert.equal(nativeCalls, 0);
  assert.equal(rotated.style.transform, 'rotate(90deg)');
  assert.equal(rotated.controls, false);
  assert.equal(rotated.currentTime, 42);
  assert.equal(rotated.paused, false);
  assert.equal(rotated.loadCalls || 0, 0);
  assert.equal(sources.length, rotatedSources);
  assert.equal(ids.get('lbVideoControls').hidden, false);
  assert(lightbox.classList.contains('immersive'));
  assert.equal(ids.get('lbFullscreen').attrs['aria-pressed'], 'true');
  const hideControls = timers.get(run('lbControlsTimer')).callback;
  hideControls();
  assert(lightbox.classList.contains('controls-hidden'));
  viewport.emit('click');
  assert(!lightbox.classList.contains('controls-hidden'));
  await ids.get('lbPlay').onclick();
  assert(rotated.paused);
  await ids.get('lbPlay').onclick();
  assert(!rotated.paused);
  ids.get('lbSeek').value = '500'; ids.get('lbSeek').oninput();
  assert.equal(rotated.currentTime, 60);
  ids.get('lbMute').onclick();
  assert.equal(rotated.muted, true);
  await run('toggleFullscreen()');
  assert(!lightbox.classList.contains('immersive'));
  assert.equal(rotated.style.transform, 'rotate(90deg)');
  lightbox.requestFullscreen = async () => { throw new Error('iPhone element fullscreen unavailable'); };
  await run('toggleFullscreen()');
  assert.equal(nativeCalls, 0);
  assert(lightbox.classList.contains('immersive'));
  assert.equal(rotated.currentTime, 60);
  run('closeLightbox()');
  delete lightbox.requestFullscreen;
  assert(!lightbox.classList.contains('immersive'));
  console.log('PASS: iPhone rotated fullscreen keeps angle and playback, avoids native fullscreen, and retains playback controls');

  run(`openLightbox([0,1].map(i=>({type:'video',root:0,path:'gesture'+i+'.mp4'})),0);`);
  const gestureVideo = run('currentVideo()');
  gestureVideo.currentTime = 20;
  gestureVideo.paused = false;
  gestureVideo.playbackRate = 1.5;
  const pointer = (type, x = 320, y = 300, extra = {}) => viewport.emit(type,
    {pointerId: 1, button: 0, isPrimary: true, clientX: x, clientY: y, ...extra});
  const holdSources = sources.length;
  pointer('pointerdown');
  assert.equal(gestureVideo.currentTime, 20);
  assert.equal(gestureVideo.playbackRate, 1.5);
  assert.equal(timers.get(run('lbHoldTimer')).delay, 300);
  timers.get(run('lbHoldTimer')).callback();
  assert.equal(gestureVideo.playbackRate, 2);
  assert.equal(gestureVideo.currentTime, 20);
  assert.equal(gestureVideo.paused, false);
  assert.equal(ids.get('lbSpeedHint').hidden, false);
  assert.equal(ids.get('lbRate').value, '1.5');
  pointer('pointermove', 100); // Moving inside the video after activation keeps acceleration.
  assert.equal(gestureVideo.playbackRate, 2);
  pointer('pointerup');
  assert.equal(gestureVideo.playbackRate, 1.5);
  assert.equal(gestureVideo.paused, false);
  assert.equal(ids.get('lbSpeedHint').hidden, true);
  viewport.emit('click', {clientX:320,clientY:300});
  assert.equal(run('lbLastTap'), null);
  assert.equal(sources.length, holdSources);
  assert.equal(gestureVideo.loadCalls || 0, 0);
  console.log('PASS: 300ms hold accelerates inline video without seeking/reloading/pausing; release restores 1.5x and suppresses the follow-up click');

  gestureVideo.paused = true; gestureVideo.playbackRate = 0.5;
  pointer('pointerdown', 50);
  timers.get(run('lbHoldTimer')).callback();
  assert.equal(gestureVideo.playbackRate, 2);
  assert.equal(gestureVideo.paused, false);
  pointer('pointercancel', 50);
  assert.equal(gestureVideo.playbackRate, 0.5);
  assert.equal(gestureVideo.paused, true);
  for (const event of ['pointercancel', 'lostpointercapture', 'pointerleave']) {
    gestureVideo.paused = false; gestureVideo.playbackRate = 1.75;
    pointer('pointerdown', 200, 200, {pointerType:'touch'});
    timers.get(run('lbHoldTimer')).callback();
    pointer(event, 200, 200, {pointerType:'touch'});
    assert.equal(gestureVideo.playbackRate, 1.75);
    assert.equal(ids.get('lbSpeedHint').hidden, true);
  }
  pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
  pointer('pointermove', 401); // Outside the actual video bounds while captured.
  assert.equal(gestureVideo.playbackRate, 1.75);
  pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
  viewport.emit('touchmove', {touches:[{clientX:200,clientY:701}],cancelable:true,preventDefault(){}});
  assert.equal(gestureVideo.playbackRate, 1.75);
  pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
  pointer('pointermove', 320, 300, {pointerType:'mouse',buttons:0});
  assert.equal(gestureVideo.playbackRate, 1.75);
  console.log('PASS: touch/mouse release, cancel, loss of capture and leaving the video restore the original rate; a paused video returns to pause');

  pointer('pointerdown');
  const cancelledHold = timers.get(run('lbHoldTimer')).callback;
  pointer('pointermove', 150);
  cancelledHold();
  assert.equal(gestureVideo.playbackRate, 1.75);
  assert.equal(run('lbHoldSpeed'), null);
  pointer('pointerdown');
  const shortPress = timers.get(run('lbHoldTimer')).callback;
  pointer('pointerup'); shortPress();
  assert.equal(gestureVideo.playbackRate, 1.75);
  pointer('pointerdown', 320, 300, {button:2});
  assert.equal(run('lbHoldTimer'), null);
  pointer('pointerdown');
  const multiTouchHold = timers.get(run('lbHoldTimer')).callback;
  pointer('pointerdown', 80, 300, {pointerId:2,isPrimary:false});
  multiTouchHold();
  assert.equal(gestureVideo.playbackRate, 1.75);
  pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
  viewport.emit('touchstart', {touches:[{clientX:200,clientY:200},{clientX:250,clientY:250}]});
  assert.equal(gestureVideo.playbackRate, 1.75);
  let menuPrevented = false;
  viewport.emit('contextmenu', {preventDefault(){menuPrevented=true;}});
  assert(menuPrevented);
  console.log('PASS: short clicks, drag gestures, right click and multitouch cannot leave a delayed 2x activation behind');

  run('lbIgnoreTapUntil=0; clearVideoTap();');
  gestureVideo.paused = true;
  gestureVideo.requireUserGesture = true;
  viewport.emit('click', {clientX:300,clientY:300});
  assert.equal(gestureVideo.paused, false, 'Audible video must start inside the click, before returning to the event loop');
  await Promise.resolve();
  assert.equal(gestureVideo.paused, false);
  run('clearVideoTap();');
  viewport.emit('click', {clientX:300,clientY:300});
  assert.equal(gestureVideo.paused, true);
  run('clearVideoTap();'); gestureVideo.paused = false;
  viewport.emit('click', {clientX:300,clientY:300});
  pointer('pointerdown', 302, 301); pointer('pointerup', 302, 301);
  viewport.emit('click', {clientX:302,clientY:301,detail:2});
  assert.equal(run('lbImmersive'), true);
  assert.equal(gestureVideo.paused, false);
  viewport.emit('dblclick', {clientX:302,clientY:301,preventDefault(){}});
  assert.equal(run('lbImmersive'), true); // Browser dblclick doesn't toggle a second time.
  run('setImmersive(false); clearVideoTap();'); gestureVideo.paused = true;
  viewport.emit('click', {clientX:300,clientY:300,detail:1});
  assert.equal(gestureVideo.paused, false);
  viewport.emit('click', {clientX:302,clientY:301,detail:1}); // iOS may send two single clicks.
  assert.equal(run('lbImmersive'), true);
  assert.equal(gestureVideo.paused, true, 'Double tap preserves the paused state from before the first click');
  run('clearVideoTap();');
  await run('toggleVideoPlayback()'); // An untrusted/timer call remains blocked by the simulated Safari policy.
  assert.equal(gestureVideo.paused, true);
  assert(ids.get('lbNotice').textContent.includes('播放按钮'));
  viewport.emit('click', {clientX:300,clientY:300});
  await Promise.resolve();
  assert.equal(gestureVideo.paused, false);
  gestureVideo.requireUserGesture = false;
  console.log('PASS: audible video starts directly in a user click; double taps preserve playback state and a blocked start can be retried');
  gestureVideo.currentTime = 115;
  ids.get('lbForward10').onclick();
  assert.equal(gestureVideo.currentTime, 120);
  ids.get('lbBack10').onclick();
  assert.equal(gestureVideo.currentTime, 110);
  ids.get('lbRate').value = '1.5'; ids.get('lbRate').onchange();
  assert.equal(gestureVideo.playbackRate, 1.5);
  ids.get('lbVolume').value = '0.25'; ids.get('lbVolume').oninput();
  assert.equal(gestureVideo.volume, 0.25);
  ids.get('lbSeek').value = '500'; ids.get('lbSeek').oninput();
  assert.equal(gestureVideo.currentTime, 60);
  console.log('PASS: single click plays/pauses; double click toggles fullscreen once; scrubbing, 10s buttons, rate and volume controls remain independent');

  for (const [code, message] of [[2, 'Wi-Fi'], [3, '解码'], [4, '格式']]) {
    gestureVideo.error = {code}; gestureVideo.emit('error');
    assert(ids.get('lbNotice').textContent.includes(message));
  }
  const previousNotice = ids.get('lbNotice').textContent;
  gestureVideo.error = {code:1}; gestureVideo.emit('error');
  assert.equal(ids.get('lbNotice').textContent, previousNotice);
  gestureVideo.error = null;
  const originalPlay = gestureVideo.play;
  gestureVideo.paused = true; ids.get('lbNotice').hidden = true;
  gestureVideo.play = async () => { throw Object.assign(new Error('Pause interrupted play'), {name:'AbortError'}); };
  await ids.get('lbPlay').onclick();
  assert.equal(ids.get('lbNotice').hidden, true);
  gestureVideo.play = originalPlay; gestureVideo.paused = false;
  console.log('PASS: loading, decoding and unsupported-format errors are distinguished; an interrupted play request does not display a false error');

  for (const action of ['rotateCurrent()', 'resizeLightbox()', 'setImmersive(false)']) {
    pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
    run(action);
    assert.equal(gestureVideo.playbackRate, 1.5);
  }
  pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
  run('go(1); finishSwitch();');
  assert.equal(gestureVideo.playbackRate, 1.5);
  assert.equal(run('currentVideo().playbackRate'), 1.5);
  pointer('pointerdown');
  const staleHold = timers.get(run('lbHoldTimer')).callback;
  run('closeLightbox(); openLightbox([{type:"video",root:0,path:"new-gesture.mp4"}],0); setImmersive(true);');
  staleHold();
  assert.equal(run('currentVideo().playbackRate'), 1.5);
  pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
  const closingVideo = run('currentVideo()');
  run('closeLightbox();');
  assert.equal(closingVideo.playbackRate, 1.5);
  console.log('PASS: rotation, resize, leaving fullscreen, changing media and closing restore rates; stale callbacks cannot accelerate a new video');

  run(`openLightbox([0,1].map(i=>({type:'video',root:0,path:'pending'+i+'.mp4'})),0);`);
  const pendingVideo = run('currentVideo()');
  let rejectPending;
  pendingVideo.play = () => new Promise((resolve, reject) => { rejectPending = reject; });
  const pendingPlayback = ids.get('lbPlay').onclick();
  run('go(1); finishSwitch();');
  rejectPending(Object.assign(new Error('Old video failed'), {name:'NotSupportedError'}));
  await pendingPlayback;
  assert.equal(ids.get('lbNotice').hidden, true);
  assert.equal(run('currentVideo().paused'), true);
  run('closeLightbox();');
  console.log('PASS: a late playback failure from the previous video cannot affect the new video or display its error');

  run(`openLightbox([0,1].map(i=>({type:'video',root:0,path:'scrub'+i+'.mp4',name:'Seek '+i})),0);`);
  const scrubVideo = run('currentVideo()');
  scrubVideo.currentTime = 30; scrubVideo.paused = false;
  let actualTime = scrubVideo.currentTime;
  const seekWrites = [];
  Object.defineProperty(scrubVideo, 'currentTime', {
    get: () => actualTime, set: time => { actualTime = time; seekWrites.push(time); },
  });
  const seekRange = ids.get('lbSeek'), scrubSources = sources.length;
  seekRange.emit('pointerdown', {button:0,isPrimary:true});
  for (const value of ['400', '600', '700']) { seekRange.value=value; seekRange.oninput(); }
  scrubVideo.emit('timeupdate');
  assert.equal(scrubVideo.currentTime, 30);
  assert.equal(seekWrites.length, 0);
  assert.equal(ids.get('lbTime').textContent, '1:24');
  assert.equal(ids.get('lbSeekHint').hidden, false);
  assert.equal(seekRange.value, '700');
  seekRange.onchange(); seekRange.emit('pointerup'); seekRange.onchange();
  assert.deepEqual(seekWrites, [84]);
  assert.equal(scrubVideo.paused, false);
  assert.equal(sources.length, scrubSources);
  seekRange.emit('pointerdown', {button:0,isPrimary:true});
  seekRange.value='500'; seekRange.oninput(); seekRange.emit('pointercancel');
  assert.equal(scrubVideo.currentTime, 84);
  assert.equal(seekWrites.length, 1);
  assert.equal(seekRange.value, '700');
  console.log('PASS: dragging the timeline previews without repeated seeks; release commits once and cancellation restores the displayed position');

  scrubVideo.paused = true;
  run('setImmersive(true);');
  pointer('pointerdown', 200, 300, {pointerType:'mouse',buttons:1});
  const pendingAcceleration = timers.get(run('lbHoldTimer')).callback;
  pointer('pointermove', 240, 300, {pointerType:'mouse',buttons:1});
  pointer('pointermove', 280, 300, {pointerType:'mouse',buttons:1});
  pendingAcceleration();
  assert.equal(scrubVideo.currentTime, 84);
  assert.equal(run('lbScrub.time'), 108);
  assert.equal(run('lbHoldSpeed'), null);
  pointer('pointerup', 280, 300, {pointerType:'mouse',buttons:0});
  viewport.emit('click', {clientX:280,clientY:300});
  assert.equal(scrubVideo.currentTime, 108);
  assert.equal(scrubVideo.paused, true);
  assert.deepEqual(seekWrites, [84,108]);
  pointer('pointerdown', 200, 300, {pointerType:'touch'});
  pointer('pointermove', 100, 300, {pointerType:'touch'});
  pointer('pointercancel', 100, 300, {pointerType:'touch'});
  assert.equal(scrubVideo.currentTime, 108);
  assert.equal(seekWrites.length, 2);
  viewport.emit('touchstart', {touches:[{clientX:300,clientY:300}]});
  viewport.emit('touchmove', {touches:[{clientX:100,clientY:305}],cancelable:true,preventDefault(){}});
  assert.equal(scrubVideo.currentTime, 108);
  viewport.emit('touchend');
  assert.equal(scrubVideo.currentTime, 48);
  assert.equal(run('lbIndex'), 0);
  assert.equal(scrubVideo.paused, true);
  assert.equal(sources.length, scrubSources);
  console.log('PASS: mouse/touch fullscreen dragging seeks on release, preserves pause and the loaded video, cancels safely and suppresses hold/click conflicts');

  scrubVideo.paused=false; scrubVideo.playbackRate=1.5;
  pointer('pointerdown'); timers.get(run('lbHoldTimer')).callback();
  ids.get('lbLock').onclick();
  assert.equal(scrubVideo.playbackRate, 1.5);
  assert.equal(run('lbPlayerLocked'), true);
  assert.equal(ids.get('lbLock').attrs['aria-label'], '解锁播放器');
  assert.equal(seekRange.disabled, true);
  ids.get('lbPlay').onclick(); ids.get('lbForward10').onclick(); ids.get('lbMute').onclick();
  ids.get('lbRate').value='1.25'; ids.get('lbRate').onchange();
  ids.get('lbVolume').value='0.2'; ids.get('lbVolume').oninput();
  seekRange.value='900'; seekRange.oninput();
  pointer('pointerdown'); viewport.emit('click',{clientX:320,clientY:300});
  viewport.emit('touchstart',{touches:[{clientX:300,clientY:300}]});
  viewport.emit('touchmove',{touches:[{clientX:100,clientY:300}],cancelable:true,preventDefault(){}});
  viewport.emit('touchend');
  await run('rotateCurrent(); toggleFullscreen(); go(1);');
  assert.equal(run('lbIndex'), 0);
  assert.equal(run('lbImmersive'), true);
  assert.equal(scrubVideo.currentTime, 48);
  assert.equal(scrubVideo.paused, false);
  assert.equal(scrubVideo.playbackRate, 1.5);
  assert.equal(scrubVideo.volume, 1);
  assert.equal(scrubVideo.muted, false);
  assert.equal(run('lbHoldSpeed'), null);
  assert.equal(seekWrites.length, 3);
  ids.get('lbLock').onclick();
  assert.equal(run('lbPlayerLocked'), false);
  assert.equal(seekRange.disabled, false);
  assert.equal(ids.get('lbLockHint').hidden, true);
  assert.equal(scrubVideo.paused, false);
  console.log('PASS: locking restores temporary speed and blocks playback, seek, volume, rotation, fullscreen and navigation until unlocked');

  scrubVideo.buffered={length:2,start:i=>[0,90][i],end:i=>[60,120][i]};
  scrubVideo.emit('progress');
  assert.equal(seekRange.style['--buffer-progress'], '50%');
  scrubVideo.readyState=2; scrubVideo.emit('waiting');
  const stalled = timers.get(run('lbBufferTimer'));
  assert.equal(stalled.delay, 250); assert.equal(ids.get('lbBufferHint').hidden, true);
  stalled.callback(); assert.equal(ids.get('lbBufferHint').hidden, false);
  scrubVideo.emit('playing'); assert.equal(ids.get('lbBufferHint').hidden, true);
  scrubVideo.emit('waiting'); const oldBuffer = timers.get(run('lbBufferTimer')).callback;
  run('go(1); finishSwitch();');
  const nextBufferVideo=run('currentVideo()');
  nextBufferVideo.paused=false; nextBufferVideo.readyState=2; nextBufferVideo.emit('waiting');
  const newBufferTimer=run('lbBufferTimer');
  oldBuffer(); scrubVideo.emit('playing');
  assert.equal(ids.get('lbBufferHint').hidden, true);
  assert.equal(run('lbBufferTimer'), newBufferTimer);
  timers.get(newBufferTimer).callback(); assert.equal(ids.get('lbBufferHint').hidden, false);
  nextBufferVideo.emit('pause'); assert.equal(ids.get('lbBufferHint').hidden, true);
  ids.get('lbLock').onclick(); run('closeLightbox();');
  assert.equal(run('lbPlayerLocked'), false);
  run(`openLightbox([{type:'image',root:0,path:'after-lock.jpg'}],0);`);
  assert.equal(ids.get('lbLock').hidden, true);
  run('closeLightbox();');
  console.log('PASS: buffer progress respects gaps, buffering feedback is delayed, stale video events cannot affect the next video and closing clears lock state');

  const grid = ids.get('grid');
  const images = Array.from({ length: 6 }, (_, index) => {
    const image = new Element('img'); image.dataset.src = '/thumb?id=' + index;
    grid.appendChild(image); return image;
  });
  const before = sources.length;
  run('lazyObserve(grid)'); observer.visible();
  assert.equal(sources.length - before, 3);
  images[0].emit('load');
  assert.equal(sources.length - before, 4);
  run('resetThumbnailLoading()');
  assert.equal(images.filter(image => image.src).length, 1);
  console.log('PASS: thumbnail requests are limited to three at a time');

  const pages = [0, 1].map(page => ({ items: [0, 1].map(index => ({
    type: 'image', root: 0, root_name: 'test', path: `p${page}${index}.jpg`, name: 'photo',
  })), total: 4, count: 2, scanning: false }));
  const urls = [];
  context.fetch = async url => {
    urls.push(url);
    return { status: 200, ok: true, json: async () => pages.shift() };
  };
  run(`view={mode:'all',root:null,path:''}; all.loading=false;`);
  await run('loadAll(true)');
  const firstTile = grid.children[0];
  await run('loadAll(false)');
  assert.equal(grid.children.length, 4);
  assert.strictEqual(grid.children[0], firstTile);
  assert(urls.every(url => url.includes('background=1')));
  console.log('PASS: pagination appends tiles without recreating existing ones');

  const resolvers = [];
  context.fetch = url => new Promise(resolve => resolvers.push({ url, resolve }));
  const earlier = run(`openFolder(0,'old')`);
  const later = run(`openFolder(1,'new')`);
  const respond = (entry, name) => entry.resolve({ status: 200, ok: true,
    json: async () => ({ folders: [], items: [], breadcrumbs: [{ name, path: '' }] }) });
  respond(resolvers[1], 'new-folder'); await later;
  respond(resolvers[0], 'old-folder'); await earlier;
  assert.equal(run('breadcrumbs[0].name'), 'new-folder');
  console.log('PASS: late responses cannot replace the newly selected folder');

  const filterRequests = [];
  context.fetch = url => new Promise(resolve => filterRequests.push({url,resolve}));
  run('mediaFilter="all";');
  const filterFolder = run('openFolder(0, "album")');
  filterRequests[0].resolve({status:200,ok:true,json:async()=>({
    folders:[{name:'nested',count:null}],items:[{type:'image',path:'photo.jpg',name:'photo'}],breadcrumbs:[],scanning:true})});
  await filterFolder;
  const countTile = grid.children[0], mediaTile = grid.children[1];
  run('updateFolderCounts([{name:"nested",count:7,image_count:5,video_count:2}]);');
  assert.strictEqual(grid.children[0], countTile);
  assert.strictEqual(grid.children[1], mediaTile);
  assert.equal(run('folderCountNodes.get("nested").textContent'), '图片 5\n视频 2');
  const pictureFilter = run('selectMediaFilter("image")');
  const videoFilter = run('selectMediaFilter("video")');
  assert(filterRequests[1].url.includes('type=image'));
  assert(filterRequests[2].url.includes('type=video'));
  filterRequests[2].resolve({status:200,ok:true,json:async()=>({
    folders:[{name:'nested',count:7}],items:[{type:'video',path:'clip.mp4',name:'clip'}],breadcrumbs:[],scanning:false})});
  await videoFilter;
  filterRequests[1].resolve({status:200,ok:true,json:async()=>({folders:[],items:[],breadcrumbs:[],scanning:false})});
  await pictureFilter;
  assert.equal(run('items[0].type'), 'video');
  assert.equal(run('mediaFilter'), 'video');
  const allVideos = run('openAll()');
  assert(filterRequests[3].url.includes('type=video'));
  filterRequests[3].resolve({status:200,ok:true,json:async()=>({items:[],total:0,count:120,scanning:false})});
  await allVideos;
  assert(ids.get('empty').textContent.includes('视频'));
  console.log('PASS: folder counts update without reloading thumbnails; type filters survive navigation and reject stale filter responses');

  run(`view={mode:'folder',root:0,path:'album'}; mediaFilter='all';
       folders=[{name:'nested',path:'album/nested',count:3,image_count:2,video_count:1}]; items=[]; renderGrid();`);
  const selectableCard = grid.children[0];
  ids.get('selectFoldersBtn').onclick(); selectableCard.onclick();
  assert.equal(run('selectedFolders.size'), 1);
  assert(selectableCard.classList.contains('folder-selected'));
  assert.equal(ids.get('hideFoldersBtn').disabled, false);
  run('updateFolderCounts([{name:"nested",count:4,image_count:3,video_count:1}]);');
  assert.strictEqual(grid.children[0], selectableCard);
  assert(selectableCard.classList.contains('folder-selected'));
  const visibilityRequests = [];
  context.fetch = async (url, options) => {
    visibilityRequests.push({url,options});
    const response = url === '/api/hidden-folders' ? {folders:JSON.parse(options.body).action === 'hide' ?
      [{root:0,path:'album/nested',name:'nested',root_name:'test'}] : []} :
      url === '/api/roots' ? {roots:[{id:0,name:'test',image_count:1,video_count:0}]} :
      {folders:[],items:[],breadcrumbs:[],scanning:false};
    return {status:200,ok:true,json:async()=>response};
  };
  await ids.get('hideFoldersBtn').onclick();
  assert.deepEqual(JSON.parse(visibilityRequests[0].options.body),
    {action:'hide',folders:[{root:0,path:'album/nested'}]});
  assert.equal(run('selectingFolders'), false);
  assert.equal(run('selectedFolders.size'), 0);
  assert.equal(run('hiddenFolders.length'), 1);
  assert.equal(grid.children.length, 0);
  const restoreButton = ids.get('hiddenFoldersList').children[0].children[1];
  await restoreButton.onclick();
  assert.equal(run('hiddenFolders.length'), 0);
  assert.equal(ids.get('restoreAllFolders').disabled, true);
  run(`roots=[{id:0,name:'test',path:'test',image_count:4,video_count:2}]; openHome();`);
  const rootCard = grid.children[1];
  ids.get('selectFoldersBtn').onclick(); rootCard.onclick();
  assert.equal(run('[...selectedFolders.values()][0].path'), '');
  console.log('PASS: folder/root selection hides the intended paths, preserves selection while counts update, and supports restoring hidden folders');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
