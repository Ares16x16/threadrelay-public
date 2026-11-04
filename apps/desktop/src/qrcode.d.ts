declare module 'qrcode' {
  const QRCode: { toCanvas(canvas: HTMLCanvasElement, value: string, options: {errorCorrectionLevel:string; margin:number; width:number}): Promise<void> };
  export default QRCode;
}
