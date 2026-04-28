/**
 * Fixture corpus for Pulse Lab.
 *
 * 50 synthetic events designed to *demonstrate* state-aware retrieval.
 * Themes cluster: work-anxiety, body/illness, intimacy, creative work,
 * fights, calm/recovery, social-belonging, fear, joy. Each event carries
 * a Plutchik-10 emotion vector + tags + ts (relative days_ago).
 *
 * Real corpora would come from user conversations via Pulse `/ingest`.
 * Lab uses fixture so the demo works zero-config.
 */

export interface FixtureEvent {
  id: number;
  text: string;
  ts_days_ago: number;
  emotions: Partial<Record<Emotion, number>>; // 0..1
  valence: number; // -1..1
  salience: number; // 0..1, anchor=1
  anchor?: boolean;
  tags: string[];
}

export type Emotion =
  | 'joy' | 'sadness' | 'anger' | 'fear' | 'trust'
  | 'disgust' | 'anticipation' | 'surprise' | 'shame' | 'guilt';

export const FIXTURE: FixtureEvent[] = [
  // ── Work / output anxiety cluster ────────────────────────
  { id: 1, text: 'кодил весь день, в шесть вечера ощущение что ничего не сделал', ts_days_ago: 0,
    emotions: { sadness: 0.5, fear: 0.3, shame: 0.4 }, valence: -0.4, salience: 0.5,
    tags: ['work', 'output-doubt', 'fatigue'] },
  { id: 2, text: 'отослал пулл реквест, никто не комментирует, проверяю каждые 10 минут', ts_days_ago: 2,
    emotions: { fear: 0.6, anticipation: 0.5, shame: 0.3 }, valence: -0.3, salience: 0.4,
    tags: ['work', 'social', 'attention'] },
  { id: 3, text: 'продакт-ревью прошло хорошо, тимлид сказал «отличная работа»', ts_days_ago: 5,
    emotions: { joy: 0.7, trust: 0.6, surprise: 0.3 }, valence: 0.7, salience: 0.6,
    tags: ['work', 'recognition'] },
  { id: 4, text: 'дедлайн через два дня, спать не хочу но надо', ts_days_ago: 1,
    emotions: { fear: 0.7, anger: 0.3, anticipation: 0.6 }, valence: -0.4, salience: 0.5,
    tags: ['work', 'pressure', 'sleep'] },
  { id: 5, text: 'четыре часа сидел над одной функцией. наконец заработала. почему я так радуюсь мелочи?', ts_days_ago: 7,
    emotions: { joy: 0.7, surprise: 0.4, shame: 0.3 }, valence: 0.5, salience: 0.5,
    tags: ['work', 'flow', 'self-doubt'] },

  // ── Intimacy cluster ─────────────────────────────────────
  { id: 6, text: 'она впервые посмотрела на меня так как будто мы вдвоём в комнате одни', ts_days_ago: 30,
    emotions: { joy: 0.8, anticipation: 0.6, trust: 0.5 }, valence: 0.8, salience: 0.9, anchor: true,
    tags: ['intimacy', 'recognition', 'romantic'] },
  { id: 7, text: 'не повернулась когда я вошёл. в груди стало холодно. опять.', ts_days_ago: 12,
    emotions: { sadness: 0.7, fear: 0.5, shame: 0.6 }, valence: -0.7, salience: 0.7,
    tags: ['intimacy', 'rejection', 'recurring'] },
  { id: 8, text: 'мы не трогаем друг друга уже месяц. она не замечает что не трогаем.', ts_days_ago: 4,
    emotions: { sadness: 0.8, anger: 0.4, shame: 0.5 }, valence: -0.7, salience: 0.7,
    tags: ['intimacy', 'distance', 'invisibility'] },
  { id: 9, text: 'смех в темноте, мы оба засмеялись над одной шуткой одновременно', ts_days_ago: 60,
    emotions: { joy: 0.9, trust: 0.7 }, valence: 0.85, salience: 0.6,
    tags: ['intimacy', 'connection', 'humor'] },

  // ── Body / illness cluster ───────────────────────────────
  { id: 10, text: 'упал с мотоцикла на повороте, локоть в кровь', ts_days_ago: 13,
    emotions: { fear: 0.6, surprise: 0.5, shame: 0.3 }, valence: -0.3, salience: 0.7,
    tags: ['body', 'accident', 'motorcycle'] },
  { id: 11, text: 'почему-то весело упал. тело сказало «живой»', ts_days_ago: 13,
    emotions: { joy: 0.6, surprise: 0.7, fear: 0.4 }, valence: 0.4, salience: 0.7,
    tags: ['body', 'aliveness', 'paradox'] },
  { id: 12, text: 'спал четыре часа, голова как ватная, не мог собрать мысль', ts_days_ago: 1,
    emotions: { sadness: 0.4, anger: 0.3, fear: 0.3 }, valence: -0.4, salience: 0.4,
    tags: ['body', 'sleep', 'fatigue'] },
  { id: 13, text: 'после первой пробежки за месяц — впервые лёгкость в груди', ts_days_ago: 15,
    emotions: { joy: 0.8, surprise: 0.5, trust: 0.4 }, valence: 0.7, salience: 0.5,
    tags: ['body', 'recovery', 'sport'] },

  // ── Anger / fights cluster ───────────────────────────────
  { id: 14, text: 'она огрызнулась когда я подошёл с чаем. молча развернулся', ts_days_ago: 8,
    emotions: { anger: 0.5, sadness: 0.6, shame: 0.4 }, valence: -0.6, salience: 0.5,
    tags: ['conflict', 'rejection', 'silence'] },
  { id: 15, text: 'опять кричала про мусор. в этот раз не молчал, ответил', ts_days_ago: 3,
    emotions: { anger: 0.7, fear: 0.3, surprise: 0.3 }, valence: -0.4, salience: 0.5,
    tags: ['conflict', 'voice', 'breakthrough'] },
  { id: 16, text: 'ушёл хлопнув дверью. полчаса сидел в машине, потом написал «прости»', ts_days_ago: 9,
    emotions: { anger: 0.6, shame: 0.5, sadness: 0.5 }, valence: -0.5, salience: 0.5,
    tags: ['conflict', 'repair', 'shame'] },

  // ── Fear / dread cluster ─────────────────────────────────
  { id: 17, text: 'три ночи подряд просыпаюсь в три. сердце стучит в горле', ts_days_ago: 2,
    emotions: { fear: 0.85, anticipation: 0.5 }, valence: -0.7, salience: 0.7,
    tags: ['anxiety', 'sleep', 'recurring'] },
  { id: 18, text: 'сел за код после двух недель пропуска. страх что ничего не вспомню', ts_days_ago: 5,
    emotions: { fear: 0.7, shame: 0.5 }, valence: -0.5, salience: 0.5,
    tags: ['work', 'return', 'incompetence-fear'] },
  { id: 19, text: 'пришло уведомление от налоговой. открыть не могу второй день', ts_days_ago: 6,
    emotions: { fear: 0.7, shame: 0.6, anticipation: 0.4 }, valence: -0.7, salience: 0.5,
    tags: ['avoidance', 'admin', 'procrastination'] },

  // ── Joy / gratitude cluster ──────────────────────────────
  { id: 20, text: 'друг написал «думал о тебе сегодня». ничего не делал, просто хорошо', ts_days_ago: 4,
    emotions: { joy: 0.8, trust: 0.7, surprise: 0.5 }, valence: 0.8, salience: 0.6,
    tags: ['friendship', 'recognition', 'unprovoked'] },
  { id: 21, text: 'дочка принесла рисунок где мы трое. без повода. постоял минуту молча', ts_days_ago: 10,
    emotions: { joy: 0.85, sadness: 0.4, surprise: 0.5 }, valence: 0.7, salience: 0.8, anchor: true,
    tags: ['family', 'child', 'gift'] },
  { id: 22, text: 'утренний кофе на балконе, ни одной мысли в голове, минут десять', ts_days_ago: 2,
    emotions: { joy: 0.6, trust: 0.6 }, valence: 0.6, salience: 0.4,
    tags: ['solitude', 'quiet', 'morning'] },

  // ── Childhood / origin echoes ────────────────────────────
  { id: 23, text: 'вспомнил как в пять лет прятался в коробке за ёлкой когда выкликнули принцем', ts_days_ago: 90,
    emotions: { fear: 0.8, shame: 0.85, sadness: 0.6 }, valence: -0.7, salience: 1.0, anchor: true,
    tags: ['childhood', 'shame-core', 'visibility'] },
  { id: 24, text: 'отец в пять лет приковал себя цепью «как собачка», смеясь. мама не была дома', ts_days_ago: 120,
    emotions: { surprise: 0.7, fear: 0.6, sadness: 0.5 }, valence: -0.4, salience: 0.95, anchor: true,
    tags: ['childhood', 'father', 'puzzle'] },
  { id: 25, text: 'мама всегда спрашивала «а ты уверен?» когда что-то хотел', ts_days_ago: 200,
    emotions: { sadness: 0.6, anger: 0.4, shame: 0.6 }, valence: -0.5, salience: 0.85, anchor: true,
    tags: ['childhood', 'mother', 'wanting'] },

  // ── Work flow + creative ─────────────────────────────────
  { id: 26, text: 'три часа в одной задаче, не заметил как стемнело', ts_days_ago: 11,
    emotions: { joy: 0.6, anticipation: 0.5 }, valence: 0.5, salience: 0.5,
    tags: ['work', 'flow', 'absorption'] },
  { id: 27, text: 'композиция наконец сложилась после месяца. слушал её четыре раза подряд', ts_days_ago: 25,
    emotions: { joy: 0.85, surprise: 0.6, trust: 0.5 }, valence: 0.85, salience: 0.7,
    tags: ['music', 'creative', 'breakthrough'] },
  { id: 28, text: 'удалил ветку которую полгода вёл. месяц боялся это сделать', ts_days_ago: 14,
    emotions: { sadness: 0.5, joy: 0.4, surprise: 0.4 }, valence: 0.1, salience: 0.5,
    tags: ['work', 'letting-go', 'ambivalence'] },

  // ── Social belonging ─────────────────────────────────────
  { id: 29, text: 'на dinner сидел молча, никто не заметил что молчал. странное облегчение', ts_days_ago: 6,
    emotions: { sadness: 0.5, trust: 0.4 }, valence: 0.0, salience: 0.4,
    tags: ['social', 'invisibility', 'paradox'] },
  { id: 30, text: 'первый раз за год сказал в группе вслух «я не понял». никто не засмеялся', ts_days_ago: 18,
    emotions: { fear: 0.5, surprise: 0.6, joy: 0.5, trust: 0.5 }, valence: 0.4, salience: 0.6,
    tags: ['social', 'voice', 'safety'] },

  // ── Money / status ───────────────────────────────────────
  { id: 31, text: 'отказался от консалтинг-задачи которая дала бы 5к. впервые без вины', ts_days_ago: 22,
    emotions: { joy: 0.5, fear: 0.4, surprise: 0.4 }, valence: 0.3, salience: 0.6,
    tags: ['money', 'boundary', 'work'] },
  { id: 32, text: 'счёт в банке упал ниже психологической черты. целый день не открывал', ts_days_ago: 3,
    emotions: { fear: 0.8, shame: 0.6 }, valence: -0.7, salience: 0.6,
    tags: ['money', 'avoidance', 'shame'] },

  // ── Loss / grief ─────────────────────────────────────────
  { id: 33, text: 'годовщина смерти деда. с утра в горле комок без причины', ts_days_ago: 0,
    emotions: { sadness: 0.85, surprise: 0.4 }, valence: -0.6, salience: 0.85, anchor: true,
    tags: ['grief', 'anniversary', 'body-knows'] },
  { id: 34, text: 'разбирал коробки на чердаке. нашёл его часы. час не мог встать', ts_days_ago: 40,
    emotions: { sadness: 0.85, surprise: 0.5, joy: 0.3 }, valence: -0.3, salience: 0.7,
    tags: ['grief', 'memory', 'object'] },

  // ── Praise + shame interplay ─────────────────────────────
  { id: 35, text: 'клиент написал «вы спасли мне неделю». ответил «да ладно, мелочь». отдёрнулся.', ts_days_ago: 8,
    emotions: { joy: 0.4, shame: 0.7, surprise: 0.4 }, valence: 0.0, salience: 0.6,
    tags: ['praise', 'dismissal', 'pattern'] },
  { id: 36, text: 'жена сказала «у тебя сегодня глаза горят». сразу подумал «не заслужил»', ts_days_ago: 17,
    emotions: { joy: 0.5, shame: 0.7 }, valence: -0.1, salience: 0.6,
    tags: ['praise', 'unworthy', 'reflex'] },

  // ── Connection through music ─────────────────────────────
  { id: 37, text: 'включил Болеро Равеля, на седьмой минуте плакал. не понял почему', ts_days_ago: 28,
    emotions: { sadness: 0.7, joy: 0.5, surprise: 0.6 }, valence: 0.0, salience: 0.7,
    tags: ['music', 'tears', 'cathartic'] },
  { id: 38, text: 'нашёл свой старый трек 2012 года. узнал себя 15-летнего. горло сжало', ts_days_ago: 35,
    emotions: { sadness: 0.65, joy: 0.5, surprise: 0.6 }, valence: 0.1, salience: 0.7,
    tags: ['music', 'past-self', 'recognition'] },

  // ── Decisions / agency ───────────────────────────────────
  { id: 39, text: 'согласился пойти на встречу куда не хотел. знал что соглашусь ещё до вопроса', ts_days_ago: 5,
    emotions: { sadness: 0.5, anger: 0.4, shame: 0.5 }, valence: -0.4, salience: 0.5,
    tags: ['agency', 'people-pleasing', 'pattern'] },
  { id: 40, text: 'впервые сказал «нет» матери по телефону. потом два часа болело в груди. не пожалел', ts_days_ago: 11,
    emotions: { fear: 0.5, joy: 0.5, sadness: 0.4, surprise: 0.5 }, valence: 0.3, salience: 0.85, anchor: true,
    tags: ['boundary', 'mother', 'first-time'] },

  // ── Recovery / regulation ────────────────────────────────
  { id: 41, text: 'после трёх дней тревоги — час йоги, и в груди впервые тепло', ts_days_ago: 4,
    emotions: { joy: 0.6, trust: 0.6 }, valence: 0.6, salience: 0.4,
    tags: ['regulation', 'body', 'after-storm'] },
  { id: 42, text: 'дождь весь день. сидел у окна с книгой. ничего не успел и впервые за неделю не виноват', ts_days_ago: 16,
    emotions: { joy: 0.5, trust: 0.5, sadness: 0.3 }, valence: 0.5, salience: 0.5,
    tags: ['solitude', 'rest', 'permission'] },

  // ── Surprise + small joys ────────────────────────────────
  { id: 43, text: 'в магазине бабушка-кассирша спросила «у вас всё в порядке?». сказал «да». она «точно?»', ts_days_ago: 7,
    emotions: { surprise: 0.7, sadness: 0.5, joy: 0.4 }, valence: 0.2, salience: 0.5,
    tags: ['stranger', 'recognition', 'small'] },
  { id: 44, text: 'нашёл записку дочки в куртке: «папа я тебя люблю». стоял в раздевалке как идиот', ts_days_ago: 21,
    emotions: { joy: 0.85, sadness: 0.3, surprise: 0.6 }, valence: 0.8, salience: 0.85, anchor: true,
    tags: ['family', 'child', 'unprovoked'] },

  // ── Self-judgment cluster ────────────────────────────────
  { id: 45, text: 'опять обещал себе ложиться в одиннадцать. опять час ночи. опять ничего не успел', ts_days_ago: 1,
    emotions: { shame: 0.8, anger: 0.5, sadness: 0.5 }, valence: -0.5, salience: 0.5,
    tags: ['self-judgment', 'recurring', 'sleep'] },
  { id: 46, text: 'смотрю чужие проекты — у всех всё лучше. знаю что лжёт картинка, но всё равно щиплет', ts_days_ago: 3,
    emotions: { shame: 0.7, sadness: 0.5, anger: 0.3 }, valence: -0.5, salience: 0.4,
    tags: ['comparison', 'social-media', 'shame'] },

  // ── Ambivalence ──────────────────────────────────────────
  { id: 47, text: 'договорились на встречу с другом. за час до — не хочу. но если отменю — будет плохо', ts_days_ago: 4,
    emotions: { fear: 0.5, sadness: 0.4, anticipation: 0.4, shame: 0.4 }, valence: -0.3, salience: 0.4,
    tags: ['social', 'ambivalence', 'people-pleasing'] },
  { id: 48, text: 'хочу к ней. и в то же время не хочу. это уже не про неё, это про что-то старое', ts_days_ago: 7,
    emotions: { sadness: 0.7, anticipation: 0.5, shame: 0.5 }, valence: -0.4, salience: 0.7,
    tags: ['intimacy', 'pattern', 'old-pain'] },

  // ── Calm anchor ──────────────────────────────────────────
  { id: 49, text: 'остров на Оби, лето 2019. камень тёплый. ничего не нужно. впервые в жизни', ts_days_ago: 1300,
    emotions: { joy: 0.85, trust: 0.85 }, valence: 0.95, salience: 1.0, anchor: true,
    tags: ['anchor', 'calm', 'self'] },
  { id: 50, text: 'после долгой паузы — взял гитару. через пять минут уже плыл. оно само', ts_days_ago: 19,
    emotions: { joy: 0.8, surprise: 0.5, trust: 0.6 }, valence: 0.8, salience: 0.6,
    tags: ['music', 'return', 'flow'] },
];
