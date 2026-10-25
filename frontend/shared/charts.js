// Dependency-free charts. All charts share the same time cursor and zoom window.
const mono = '10px ui-monospace, SFMono-Regular, Consolas, monospace';
const timeLabel = ms => new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(ms);
const numberLabel = value => Number.isFinite(value) ? new Intl.NumberFormat('en-US',{maximumFractionDigits:Math.abs(value)<1?5:Math.abs(value)<100?3:2}).format(value) : '—';

export class TimeChart {
  constructor(canvas, {onHover=()=>{},onZoom=()=>{},format=numberLabel}={}) {
    this.canvas=canvas; this.ctx=canvas.getContext('2d'); this.onHover=onHover; this.onZoom=onZoom; this.format=format;
    this.times=[];this.series=[];this.cursor=null;this.zoom=null;this.drag=null;this.gapMs=Infinity;this.axisLabelWidth=66;
    this.tooltip=document.createElement('div');this.tooltip.className='chart-tooltip';this.tooltip.hidden=true;canvas.parentElement.append(this.tooltip);
    this.observer=new ResizeObserver(()=>this.draw());this.observer.observe(canvas);
    canvas.addEventListener('pointerdown',e=>{if(e.button!==0||!this.times.length)return;this.drag=this.pointerTime(e);this.dragEnd=this.drag;canvas.setPointerCapture(e.pointerId);});
    canvas.addEventListener('pointermove',e=>{if(!this.times.length)return;const t=this.pointerTime(e);this.dragEnd=t;this.onHover(t);});
    canvas.addEventListener('pointerup',e=>{if(this.drag!==null){const a=this.drag,b=this.pointerTime(e);this.drag=null;if(Math.abs(b-a)>(this.domain()[1]-this.domain()[0])*.015)this.onZoom([Math.min(a,b),Math.max(a,b)]);this.draw();}});
    canvas.addEventListener('pointercancel',()=>{this.drag=null;this.draw();});
    canvas.addEventListener('pointerleave',()=>{if(this.drag===null)this.onHover(null);});
    canvas.addEventListener('dblclick',()=>this.onZoom(null));
  }
  destroy(){this.observer.disconnect();this.tooltip.remove();}
  setData(times,series,{gapMs=Infinity}={}){this.times=times;this.series=series;this.gapMs=gapMs;this.draw();}
  setCursor(time){this.cursor=time;this.draw();}
  setZoom(range){this.zoom=range;this.draw();}
  domain(){return this.zoom||[this.times[0]||0,this.times.at(-1)||1];}
  plotArea(width){const left=Math.min(12,Math.max(0,width-1)),margin=Math.min(Math.max(82,this.axisLabelWidth+16),Math.max(0,width-left-1));return{left,right:Math.max(left+1,width-margin)};}
  pointerTime(e){const rect=this.canvas.getBoundingClientRect(),[a,b]=this.domain(),{left,right}=this.plotArea(rect.width);return a+Math.max(0,Math.min(1,(e.clientX-rect.left-left)/Math.max(1,right-left)))*(b-a);}
  draw(){
    const ctx=this.ctx;if(!ctx)return;
    const rect=this.canvas.getBoundingClientRect();const w=rect.width,h=rect.height;if(w<1||h<1)return;
    const dpr=Math.min(window.devicePixelRatio||1,2);
    if(this.canvas.width!==Math.round(w*dpr)||this.canvas.height!==Math.round(h*dpr)){this.canvas.width=Math.round(w*dpr);this.canvas.height=Math.round(h*dpr);}
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);ctx.font=mono;
    const top=18,bottom=h-29;
    const [t0,t1]=this.domain();const range=Math.max(1,t1-t0);
    const indexes=[];for(let i=0;i<this.times.length;i++)if(this.times[i]>=t0&&this.times[i]<=t1)indexes.push(i);
    let low=Infinity,high=-Infinity;
    for(const s of this.series)for(const i of indexes){const v=s.values[i];if(Number.isFinite(v)){low=Math.min(low,v);high=Math.max(high,v);}}
    if(!Number.isFinite(low)){low=0;high=1;}
    let pad=(high-low)*.14;if(!pad)pad=Math.abs(high)*.0005||.01;low-=pad;high+=pad;
    const axisLabels=Array.from({length:5},(_,i)=>String(this.format(high-(high-low)*i/4)));
    this.axisLabelWidth=Math.ceil(Math.max(...axisLabels.map(label=>{const measured=ctx.measureText?.(label)?.width;return Number.isFinite(measured)?measured:label.length*6;})));
    const {left,right}=this.plotArea(w),x=t=>left+(t-t0)/range*(right-left);
    const y=v=>bottom-(v-low)/(high-low)*(bottom-top);
    ctx.lineWidth=1;ctx.strokeStyle='#29363d';ctx.fillStyle='#718894';ctx.textAlign='left';
    for(let i=0;i<5;i++){const yy=top+(bottom-top)*i/4;ctx.beginPath();ctx.moveTo(left,yy+.5);ctx.lineTo(right,yy+.5);ctx.stroke();ctx.fillText(axisLabels[i],right+10,yy+3);}
    ctx.fillStyle='#677f8c';
    for(let i=0;i<4;i++){const xx=left+(right-left)*i/3;const t=t0+range*i/3;ctx.textAlign=i===0?'left':i===3?'right':'center';ctx.fillText(timeLabel(t),xx,bottom+21);}
    ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
    for(const s of this.series){
      ctx.strokeStyle=s.color;ctx.lineWidth=s.width||1.3;ctx.setLineDash(s.dashed?[4,5]:[]);ctx.beginPath();let previous=null;
      for(const i of indexes){const v=s.values[i];if(!Number.isFinite(v)){previous=null;continue;}const px=x(this.times[i]),py=y(v);if(previous===null||this.times[i]-this.times[previous]>this.gapMs*1.6)ctx.moveTo(px,py);else ctx.lineTo(px,py);previous=i;}ctx.stroke();
      if(indexes.length===1){const i=indexes[0],v=s.values[i];if(Number.isFinite(v)){ctx.fillStyle=s.color;ctx.beginPath();ctx.arc(x(this.times[i]),y(v),3,0,Math.PI*2);ctx.fill();}}
    }
    ctx.setLineDash([]);
    if(this.drag!==null&&Number.isFinite(this.dragEnd)){ctx.fillStyle='#5bdfb71a';ctx.fillRect(x(Math.min(this.drag,this.dragEnd)),top,Math.abs(x(this.drag)-x(this.dragEnd)),bottom-top);}
    ctx.restore();
    if(this.cursor!==null&&indexes.length&&this.cursor>=t0&&this.cursor<=t1){
      let lo=0,hi=this.times.length-1;while(lo<hi){const mid=Math.floor((lo+hi)/2);if(this.times[mid]<this.cursor)lo=mid+1;else hi=mid;}let i=lo;if(i>0&&Math.abs(this.times[i-1]-this.cursor)<Math.abs(this.times[i]-this.cursor))i--;
      i=Math.max(indexes[0],Math.min(indexes.at(-1),i));
      const xx=x(this.times[i]);ctx.strokeStyle='#72968a';ctx.lineWidth=1;ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(xx,top);ctx.lineTo(xx,bottom);ctx.stroke();ctx.setLineDash([]);
      const labels=[];for(const s of this.series){const v=s.values[i];if(!Number.isFinite(v))continue;ctx.fillStyle=s.color;ctx.beginPath();ctx.arc(xx,y(v),2.7,0,Math.PI*2);ctx.fill();labels.push(`${s.label}: ${this.format(v)}`);}
      this.tooltip.textContent=timeLabel(this.times[i])+' KST\n'+labels.join('\n');this.tooltip.hidden=false;this.tooltip.style.left=Math.max(15,Math.min(w-205,xx+12))+'px';
    }else this.tooltip.hidden=true;
  }
}
