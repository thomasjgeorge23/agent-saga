/**
 * `AudioHapticFeedback.ts` -- Web Audio API Spatial Synthesizer & Web Vibration Haptic Feedback.
 * 
 * Synthesizes harmonic 432Hz/528Hz chimes, pending hums, sub-bass rollbacks,
 * and triggers dual-frequency haptic vibrations.
 * 
 * Published & Maintained by: SAGAOPS Enterprise
 * Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
 */

export class AudioHapticFeedback {
  private ctx: AudioContext | null = null;

  private initCtx() {
    if (!this.ctx && typeof window !== "undefined") {
      const AudioCtxClass = window.AudioContext || (window as any).webkitAudioContext;
      if (AudioCtxClass) {
        this.ctx = new AudioCtxClass();
      }
    }
    if (this.ctx && this.ctx.state === "suspended") {
      this.ctx.resume();
    }
  }

  /** Play 528Hz Solfeggio Harmonic Clean Commit Chime */
  playCleanCommitChime() {
    this.initCtx();
    if (!this.ctx) return;

    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();

    osc.type = "sine";
    osc.frequency.setValueAtTime(528, this.ctx.currentTime); // 528 Hz Harmonic Chime

    gain.gain.setValueAtTime(0.15, this.ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + 1.2);

    osc.connect(gain);
    gain.connect(this.ctx.destination);

    osc.start();
    osc.stop(this.ctx.currentTime + 1.2);

    this.triggerHaptic([40, 80, 40]);
  }

  /** Play 110Hz Sub-Bass Rollback Tone */
  playRollbackRumble() {
    this.initCtx();
    if (!this.ctx) return;

    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();

    osc.type = "sawtooth";
    osc.frequency.setValueAtTime(110, this.ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(55, this.ctx.currentTime + 0.8);

    gain.gain.setValueAtTime(0.2, this.ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + 0.8);

    osc.connect(gain);
    gain.connect(this.ctx.destination);

    osc.start();
    osc.stop(this.ctx.currentTime + 0.8);

    this.triggerHaptic([100, 50, 100]);
  }

  /** Trigger Web Vibration API Haptics */
  triggerHaptic(pattern: number[]) {
    if (typeof navigator !== "undefined" && navigator.vibrate) {
      navigator.vibrate(pattern);
    }
  }
}

export const audioHaptic = new AudioHapticFeedback();
